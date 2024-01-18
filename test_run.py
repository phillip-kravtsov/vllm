from openai import OpenAI

openai_api_key = "EMPTY"
openai_api_base = "http://localhost:8000/v1"

client = OpenAI(
    api_key=openai_api_key,
    base_url=openai_api_base,
)
chat_model = "facebook/opt-125m"
chat_prompt = "".join(["""A Tale of Two Cities is a historical novel published in 1859 by Charles Dickens, set in London and Paris before and during the French Revolution. The novel tells the story of the French Doctor Manette, his 18-year-long imprisonment in the Bastille in Paris, and his release to live in London with his daughter Lucie whom he had never met. The story is set against the conditions that led up to the French Revolution and the Reign of Terror.

As Dickens's best-known work of historical fiction, A Tale of Two Cities is said to be one of the best-selling novels of all time.[2][3][4] In 2003, the novel was ranked 63rd on the BBC's The Big Read poll.[5] The novel has been adapted for film, television, radio, and the stage, and has continued to influence popular culture.

Synopsis
Book the First: Recalled to Life
Opening lines
Dickens opens the novel with a sentence that has become famous:

It was the best of times, it was the worst of times, it was the age of wisdom, it was the age of foolishness, it was the epoch of belief, it was the epoch of incredulity, it was the season of Light, it was the season of Darkness, it was the spring of hope, it was the winter of despair, we had everything before us, we had nothing before us, we were all going direct to Heaven, we were all going direct the other way—in short, the period was so far like the present period, that some of its noisiest authorities insisted on its being received, for good or for evil,"""] * 5)
suffix_prompt = chat_prompt + " in the superlative degree of comparison only."
client = OpenAI(
    api_key=openai_api_key,
    base_url=openai_api_base,
)
import time
start_time = time.time()
first_response = client.completions.create(model="facebook/opt-125m",
                                      prompt=suffix_prompt, temperature=0.1)
# second response is a substr of the first one
#print(suffix_prompt + first_response.choices[0].text)
second_response = client.completions.create(model="facebook/opt-125m",
                                      prompt=chat_prompt, temperature=0.1)
#print(chat_prompt + second_response.choices[0].text)                                    
print(first_response.usage)
print(second_response.usage)
print()
print("Time taken for first:", time.time() - start_time)                                    
print('Time taken for second:', time.time() - start_time)
