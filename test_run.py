from openai import OpenAI

openai_api_key = "EMPTY"
openai_api_base = "http://localhost:8000/v1"

client = OpenAI(
    api_key=openai_api_key,
    base_url=openai_api_base,
)
chat_model = "facebook/opt-125m"
chat_prompt = "San Francisco is a small city on the coast of California. It is known"
suffix_prompt = chat_prompt + " for its beautiful Golden Gate Bridge. The park closest to the bridge is called"
client = OpenAI(
    api_key=openai_api_key,
    base_url=openai_api_base,
)
first_response = client.completions.create(model="facebook/opt-125m",
                                      prompt=suffix_prompt)
# second response is a substr of the first one
second_response = client.completions.create(model="facebook/opt-125m",
                                      prompt=chat_prompt)
third = client.completions.create(model="facebook/opt-125m",
                                      prompt='totally unrelated')
print("First result:", first_response)
print()
print("Folllowup result:", second_response)
