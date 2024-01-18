from openai import OpenAI

openai_api_key = "EMPTY"
openai_api_base = "http://localhost:8000/v1"

client = OpenAI(
    api_key=openai_api_key,
    base_url=openai_api_base,
)
chat_model = "facebook/opt-125m"
chat_prompt = "San Francisco is a"
client = OpenAI(
    api_key=openai_api_key,
    base_url=openai_api_base,
)
first_response = client.completions.create(model="facebook/opt-125m",
                                      prompt=chat_prompt)
second_response = client.completions.create(model="facebook/opt-125m",
                                      prompt=chat_prompt + " small city on the coast of")
print("First result:", first_response)
print()
print("Folllowup result:", second_response)
