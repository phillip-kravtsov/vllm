from collections import deque
import enum
import time
from typing import Deque, Dict, Iterable, List, Optional, Tuple, Union, Set

from vllm.config import CacheConfig, SchedulerConfig
from vllm.core.block_manager import AllocStatus, BlockSpaceManager
from vllm.core.policy import PolicyFactory
from vllm.logger import init_logger
from vllm.sequence import (Sequence, SequenceData, SequenceGroup,
                           SequenceGroupMetadata, SequenceStatus)

logger = init_logger(__name__)


class PreemptionMode(enum.Enum):
    """Preemption modes.

    1. Swapping: Swap out the blocks of the preempted sequences to CPU memory
    and swap them back in when the sequences are resumed.
    2. Recomputation: Discard the blocks of the preempted sequences and
    recompute them when the sequences are resumed, treating the sequences as
    new prompts.
    """
    SWAP = enum.auto()
    RECOMPUTE = enum.auto()


class SchedulerOutputs:

    def __init__(
        self,
        scheduled_seq_groups: Iterable[SequenceGroup],
        prompt_run: bool,
        num_batched_tokens: int,
        blocks_to_swap_in: Dict[int, int],
        blocks_to_swap_out: Dict[int, int],
        blocks_to_copy: Dict[int, List[int]],
        ignored_seq_groups: List[SequenceGroup],
    ) -> None:
        self.scheduled_seq_groups = scheduled_seq_groups
        self.prompt_run = prompt_run
        self.num_batched_tokens = num_batched_tokens
        self.blocks_to_swap_in = blocks_to_swap_in
        self.blocks_to_swap_out = blocks_to_swap_out
        self.blocks_to_copy = blocks_to_copy
        # Swap in and swap out should never happen at the same time.
        assert not (blocks_to_swap_in and blocks_to_swap_out)
        self.ignored_seq_groups = ignored_seq_groups

    def is_empty(self) -> bool:
        # NOTE: We do not consider the ignored sequence groups.
        return (not self.scheduled_seq_groups and not self.blocks_to_swap_in
                and not self.blocks_to_swap_out and not self.blocks_to_copy)


class Scheduler:

    def __init__(
        self,
        scheduler_config: SchedulerConfig,
        cache_config: CacheConfig,
    ) -> None:
        self.scheduler_config = scheduler_config
        self.cache_config = cache_config

        self.prompt_limit = min(self.scheduler_config.max_model_len,
                                self.scheduler_config.max_num_batched_tokens)

        # Instantiate the scheduling policy.
        self.policy = PolicyFactory.get_policy(policy_name="fcfs")
        # Create the block space manager.
        self.block_manager = BlockSpaceManager(
            block_size=self.cache_config.block_size,
            num_gpu_blocks=self.cache_config.num_gpu_blocks,
            num_cpu_blocks=self.cache_config.num_cpu_blocks,
            sliding_window=self.cache_config.sliding_window)

        # Sequence groups in the WAITING state.
        self.waiting: Deque[SequenceGroup] = deque()
        # Sequence groups in the RUNNING state.
        self.running: Deque[SequenceGroup] = deque()
        # Sequence groups in the SWAPPED state.
        self.swapped: Deque[SequenceGroup] = deque()

        # Sequence groups that are cached -- these are used to keep kv caches warm for subsequent requests.
        self.cached: Deque[SequenceGroup] = deque()

    def add_seq_group(self, seq_group: SequenceGroup) -> None:
        # Add sequence groups to the waiting queue.
        self.waiting.append(seq_group)

    def abort_seq_group(self, request_id: Union[str, Iterable[str]]) -> None:
        """Aborts a sequence group with the given ID.

        Check if the sequence group with the given ID
            is present in any of the state queue.
        If present, remove the sequence group from the state queue.
            Also, if any of the sequences in the sequence group is not finished,
                free the sequence with status `FINISHED_ABORTED`.
        Otherwise, do nothing.

        Args:
            request_id: The ID(s) of the sequence group to abort.
        """
        if isinstance(request_id, str):
            request_id = (request_id, )
        request_ids = set(request_id)
        # TODO move finished sequences to cache
        for state_queue in [self.waiting, self.running, self.swapped]:
            aborted_groups = []
            for seq_group in state_queue:
                if not request_ids:
                    # Using 'break' here may add two extra iterations,
                    # but is acceptable to reduce complexity .
                    break
                if seq_group.request_id in request_ids:
                    # Appending aborted group into pending list.
                    aborted_groups.append(seq_group)
                    request_ids.remove(seq_group.request_id)
            
            for aborted_group in aborted_groups:
                # Remove the sequence group from the state queue.
                state_queue.remove(aborted_group)
                if self.cache_config.enable_cross_request_kv_cache_sharing:
                    self.cached.append(aborted_group)

                for seq in seq_group.get_seqs():
                    if seq.is_finished():
                        continue
                    seq.status = SequenceStatus.FINISHED_ABORTED
                    if not self.cache_config.enable_cross_request_kv_cache_sharing:
                        self.free_seq(seq)

    def has_unfinished_seqs(self) -> bool:
        return self.waiting or self.running or self.swapped

    def get_num_unfinished_seq_groups(self) -> int:
        return len(self.waiting) + len(self.running) + len(self.swapped)

    def _schedule(self) -> SchedulerOutputs:
        # Blocks that need to be swaped or copied before model execution.
        blocks_to_swap_in: Dict[int, int] = {}
        blocks_to_swap_out: Dict[int, int] = {}
        blocks_to_copy: Dict[int, List[int]] = {}
        input_ids_to_sequence: Dict[Tuple, Tuple[Sequence, SequenceGroup]] = {}
        # Fix the current time.
        now = time.monotonic()
        ttl = 10.0
        to_evict: List[SequenceGroup] = []

        for seq_group in self.cached:
            if seq_group.arrival_time < now - ttl:
                print(f'Evicting seq group with arrival time {seq_group.arrival_time:4f}, now {now:4f} with difference {now - seq_group.arrival_time:4f}')
                to_evict.append(seq_group)
            else:
                cached_sequences = seq_group.get_seqs()
                for cached_seq in cached_sequences:
                    token_ids = cached_seq.get_token_ids()
                    input_ids_to_sequence[tuple(token_ids)] = (cached_seq, seq_group)

        for evict_group in to_evict:
            self.cached.remove(evict_group)
            print('Evicting seq group')
            for seq in evict_group.get_seqs():
                self.free_seq(seq)
        if len(input_ids_to_sequence) > 0:
            print(f'There are {len(input_ids_to_sequence)} cached sequences')

        # Join waiting sequences if possible.
        if not self.swapped:
            ignored_seq_groups: List[SequenceGroup] = []
            scheduled: List[SequenceGroup] = []
            # The total number of sequences on the fly, including the
            # requests in the generation phase.
            num_curr_seqs = sum(seq_group.get_max_num_running_seqs()
                                for seq_group in self.running)
            seq_lens: List[int] = []

            # Optimization: We do not sort the waiting queue since the preempted
            # sequence groups are added to the front and the new sequence groups
            # are added to the back.
            while self.waiting:
                seq_group = self.waiting[0]

                waiting_seqs = seq_group.get_seqs(
                    status=SequenceStatus.WAITING)
                assert len(waiting_seqs) == 1, (
                    "Waiting sequence group should have only one prompt "
                    "sequence.")
                num_prompt_tokens = waiting_seqs[0].get_len()
                if num_prompt_tokens > self.prompt_limit:
                    logger.warning(
                        f"Input prompt ({num_prompt_tokens} tokens) is too long"
                        f" and exceeds limit of {self.prompt_limit}")
                    for seq in waiting_seqs:
                        seq.status = SequenceStatus.FINISHED_IGNORED
                    ignored_seq_groups.append(seq_group)
                    self.waiting.popleft()
                    continue

                # If the sequence group cannot be allocated, stop.
                can_allocate = self.block_manager.can_allocate(seq_group)
                if can_allocate == AllocStatus.LATER:
                    break
                elif can_allocate == AllocStatus.NEVER:
                    logger.warning(
                        f"Input prompt ({num_prompt_tokens} tokens) is too long"
                        f" and exceeds the capacity of block_manager")
                    for seq in waiting_seqs:
                        seq.status = SequenceStatus.FINISHED_IGNORED
                    ignored_seq_groups.append(seq_group)
                    self.waiting.popleft()
                    continue

                # If the number of batched tokens exceeds the limit, stop.
                new_seq_lens = seq_lens + [num_prompt_tokens]
                num_batched_tokens = len(new_seq_lens) * max(new_seq_lens)
                if (num_batched_tokens >
                        self.scheduler_config.max_num_batched_tokens):
                    break

                # The total number of sequences in the RUNNING state should not
                # exceed the maximum number of sequences.
                num_new_seqs = seq_group.get_max_num_running_seqs()
                if (num_curr_seqs + num_new_seqs >
                        self.scheduler_config.max_num_seqs):
                    break

                num_paddings = num_batched_tokens - sum(new_seq_lens)
                if num_paddings > self.scheduler_config.max_paddings:
                    break
                seq_lens = new_seq_lens

                seq_group = self.waiting.popleft()
                self._allocate(seq_group)
                self.running.append(seq_group)
                num_curr_seqs += num_new_seqs
                scheduled.append(seq_group)

            if scheduled and self.cache_config.enable_cross_request_kv_cache_sharing:

                sequences_to_copy: List[Tuple[Sequence, Sequence]] = []
                sequence_groups_to_remove: Set[SequenceGroup] = set()
                sequence_groups_to_copy: List[Tuple[SequenceGroup, SequenceGroup]] = []
                for seq_group in scheduled:
                    waiting_sequences = seq_group.get_seqs()
                    for waiting_seq in waiting_sequences:
                        waiting_seq_token_ids = tuple(waiting_seq.data.prompt_token_ids)
                        for cached_input_ids, (cached_seq, cached_sequence_group) in input_ids_to_sequence.items():
                            hits = [c == w for c, w in zip(cached_input_ids, waiting_seq_token_ids)]
                            print('Hits', sum(hits), len(hits), 'len(waiting)', len(waiting_seq_token_ids), 'len(cache)', len(cached_input_ids))
                            if all(hits) and len(hits) == len(waiting_seq_token_ids):
                                # we only care about a cache hit if the stuff we are about to decode is exactly 
                                # covered by the stuff we have cached.
                                print('Potential cache hit of length ', len(hits))
                                sequences_to_copy.append((cached_seq, waiting_seq))
                                sequence_groups_to_copy.append((cached_sequence_group, seq_group))
                                sequence_groups_to_remove.add(seq_group)

                # Copy the cached sequence kv cache directly to the waiting sequence.
                # We don't need to worry about non-full blocks because VLLM does not read data from them past the ctx length
                for from_sequence, to_sequence in sequences_to_copy: 
                    from_seq_physical_blocks = self.block_manager.block_tables[from_sequence.seq_id]
                    to_seq_physical_blocks = self.block_manager.block_tables[to_sequence.seq_id]
                    for physical_from_block, physical_to_block in zip(from_seq_physical_blocks, to_seq_physical_blocks):
                        if physical_from_block.block_number not in blocks_to_copy:
                            blocks_to_copy[physical_from_block.block_number] = []
                        blocks_to_copy[physical_from_block.block_number].append(physical_to_block.block_number)

                # copy the prompt logprobs from the cached sequence group to the waiting sequence group
                # this is because the waiting group never goes into the prefill stage of vllm
                for from_sequence_group, to_sequence_group in sequence_groups_to_copy:
                    # TODO ideally would handle this case more elegantly -- we should ignore this sequence group for caching and report
                    assert from_sequence_group.prompt_logprobs is not None
                    to_sequence_group.prompt_logprobs = from_sequence_group.prompt_logprobs[:len(to_sequence_group.prompt_token_ids)]
                # move the waiting sequence group to the RUNNING stage as if it just executed prefill
                for group_to_unschedule in sequence_groups_to_remove:
                    scheduled.remove(group_to_unschedule)
                    for seq in group_to_unschedule.get_seqs():
                        seq.status = SequenceStatus.RUNNING

            if scheduled or ignored_seq_groups:
                scheduler_outputs = SchedulerOutputs(
                    scheduled_seq_groups=scheduled,
                    prompt_run=True,
                    num_batched_tokens=len(seq_lens) *
                    max(seq_lens) if seq_lens else 0,
                    blocks_to_swap_in=blocks_to_swap_in,
                    blocks_to_swap_out=blocks_to_swap_out,
                    blocks_to_copy=blocks_to_copy,
                    ignored_seq_groups=ignored_seq_groups,
                )
                return scheduler_outputs

        # NOTE(woosuk): Preemption happens only when there is no available slot
        # to keep all the sequence groups in the RUNNING state.
        # In this case, the policy is responsible for deciding which sequence
        # groups to preempt.
        self.running = self.policy.sort_by_priority(now, self.running)

        # Reserve new token slots for the running sequence groups.
        running: Deque[SequenceGroup] = deque()
        preempted: List[SequenceGroup] = []
        while self.running:
            seq_group = self.running.popleft()
            while not self.block_manager.can_append_slot(seq_group):
                if self.running:
                    # Preempt the lowest-priority sequence groups.
                    victim_seq_group = self.running.pop()
                    self._preempt(victim_seq_group, blocks_to_swap_out)
                    preempted.append(victim_seq_group)
                else:
                    # No other sequence groups can be preempted.
                    # Preempt the current sequence group.
                    self._preempt(seq_group, blocks_to_swap_out)
                    preempted.append(seq_group)
                    break
            else:
                # Append new slots to the sequence group.
                self._append_slot(seq_group, blocks_to_copy)
                running.append(seq_group)
        self.running = running

        # Swap in the sequence groups in the SWAPPED state if possible.
        self.swapped = self.policy.sort_by_priority(now, self.swapped)
        if not preempted:
            num_curr_seqs = sum(seq_group.get_max_num_running_seqs()
                                for seq_group in self.running)

            while self.swapped:
                seq_group = self.swapped[0]
                # If the sequence group cannot be swapped in, stop.
                if not self.block_manager.can_swap_in(seq_group):
                    break

                # The total number of sequences in the RUNNING state should not
                # exceed the maximum number of sequences.
                num_new_seqs = seq_group.get_max_num_running_seqs()
                if (num_curr_seqs + num_new_seqs >
                        self.scheduler_config.max_num_seqs):
                    break

                seq_group = self.swapped.popleft()
                self._swap_in(seq_group, blocks_to_swap_in)
                self._append_slot(seq_group, blocks_to_copy)
                num_curr_seqs += num_new_seqs
                self.running.append(seq_group)

        # Each sequence in the generation phase only takes one token slot.
        # Therefore, the number of batched tokens is equal to the number of
        # sequences in the RUNNING state.
        num_batched_tokens = sum(
            seq_group.num_seqs(status=SequenceStatus.RUNNING)
            for seq_group in self.running)

        scheduler_outputs = SchedulerOutputs(
            scheduled_seq_groups=self.running,
            prompt_run=False,
            num_batched_tokens=num_batched_tokens,
            blocks_to_swap_in=blocks_to_swap_in,
            blocks_to_swap_out=blocks_to_swap_out,
            blocks_to_copy=blocks_to_copy,
            ignored_seq_groups=[],
        )
        return scheduler_outputs

    def schedule(self) -> Tuple[List[SequenceGroupMetadata], SchedulerOutputs]:
        # Schedule sequence groups.
        # This function call changes the internal states of the scheduler
        # such as self.running, self.swapped, and self.waiting.
        scheduler_outputs = self._schedule()

        # Create input data structures.
        seq_group_metadata_list: List[SequenceGroupMetadata] = []
        for seq_group in scheduler_outputs.scheduled_seq_groups:
            seq_data: Dict[int, SequenceData] = {}
            block_tables: Dict[int, List[int]] = {}
            running_seqs = seq_group.get_seqs(status=SequenceStatus.RUNNING)
            all_seqs = seq_group.get_seqs()
            for seq in running_seqs:
                seq_id = seq.seq_id
                seq_data[seq_id] = seq.data
                block_tables[seq_id] = self.block_manager.get_block_table(seq)

            seq_group_metadata = SequenceGroupMetadata(
                request_id=seq_group.request_id,
                is_prompt=scheduler_outputs.prompt_run,
                seq_data=seq_data,
                sampling_params=seq_group.sampling_params,
                block_tables=block_tables,
            )
            seq_group_metadata_list.append(seq_group_metadata)
        return seq_group_metadata_list, scheduler_outputs

    def fork_seq(self, parent_seq: Sequence, child_seq: Sequence) -> None:
        self.block_manager.fork(parent_seq, child_seq)

    def free_seq(self, seq: Sequence) -> None:
        self.block_manager.free(seq)

    def free_finished_seq_groups(self) -> None:
        self.running = [
            seq_group for seq_group in self.running
            if not seq_group.is_finished()
        ]

    def _allocate(self, seq_group: SequenceGroup) -> None:
        self.block_manager.allocate(seq_group)
        for seq in seq_group.get_seqs(status=SequenceStatus.WAITING):
            seq.status = SequenceStatus.RUNNING

    def _append_slot(
        self,
        seq_group: SequenceGroup,
        blocks_to_copy: Dict[int, List[int]],
    ) -> None:
        for seq in seq_group.get_seqs(status=SequenceStatus.RUNNING):
            ret = self.block_manager.append_slot(seq)
            if ret is not None:
                src_block, dst_block = ret
                if src_block in blocks_to_copy:
                    blocks_to_copy[src_block].append(dst_block)
                else:
                    blocks_to_copy[src_block] = [dst_block]

    def _preempt(
        self,
        seq_group: SequenceGroup,
        blocks_to_swap_out: Dict[int, int],
        preemption_mode: Optional[PreemptionMode] = None,
    ) -> None:
        # If preemption mode is not specified, we determine the mode as follows:
        # We use recomputation by default since it incurs lower overhead than
        # swapping. However, when the sequence group has multiple sequences
        # (e.g., beam search), recomputation is not currently supported. In
        # such a case, we use swapping instead.
        # FIXME(woosuk): This makes our scheduling policy a bit bizarre.
        # As swapped sequences are prioritized over waiting sequences,
        # sequence groups with multiple sequences are implicitly prioritized
        # over sequence groups with a single sequence.
        # TODO(woosuk): Support recomputation for sequence groups with multiple
        # sequences. This may require a more sophisticated CUDA kernel.
        if preemption_mode is None:
            if seq_group.get_max_num_running_seqs() == 1:
                preemption_mode = PreemptionMode.RECOMPUTE
            else:
                preemption_mode = PreemptionMode.SWAP
        if preemption_mode == PreemptionMode.RECOMPUTE:
            self._preempt_by_recompute(seq_group)
        elif preemption_mode == PreemptionMode.SWAP:
            self._preempt_by_swap(seq_group, blocks_to_swap_out)
        else:
            raise AssertionError("Invalid preemption mode.")

    def _preempt_by_recompute(
        self,
        seq_group: SequenceGroup,
    ) -> None:
        seqs = seq_group.get_seqs(status=SequenceStatus.RUNNING)
        assert len(seqs) == 1
        for seq in seqs:
            seq.status = SequenceStatus.WAITING
            self.block_manager.free(seq)
        # NOTE: For FCFS, we insert the preempted sequence group to the front
        # of the waiting queue.
        self.waiting.appendleft(seq_group)

    def _preempt_by_swap(
        self,
        seq_group: SequenceGroup,
        blocks_to_swap_out: Dict[int, int],
    ) -> None:
        self._swap_out(seq_group, blocks_to_swap_out)
        self.swapped.append(seq_group)

    def _swap_in(
        self,
        seq_group: SequenceGroup,
        blocks_to_swap_in: Dict[int, int],
    ) -> None:
        mapping = self.block_manager.swap_in(seq_group)
        blocks_to_swap_in.update(mapping)
        for seq in seq_group.get_seqs(status=SequenceStatus.SWAPPED):
            seq.status = SequenceStatus.RUNNING

    def _swap_out(
        self,
        seq_group: SequenceGroup,
        blocks_to_swap_out: Dict[int, int],
    ) -> None:
        if not self.block_manager.can_swap_out(seq_group):
            # FIXME(woosuk): Abort the sequence group instead of aborting the
            # entire engine.
            raise RuntimeError(
                "Aborted due to the lack of CPU swap space. Please increase "
                "the swap space to avoid this error.")
        mapping = self.block_manager.swap_out(seq_group)
        blocks_to_swap_out.update(mapping)
        for seq in seq_group.get_seqs(status=SequenceStatus.RUNNING):
            seq.status = SequenceStatus.SWAPPED
