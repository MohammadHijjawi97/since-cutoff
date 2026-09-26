import anthropic
from huggingface_hub import hf_hub_download
from langchain_core.retrievers import BaseRetriever

def summarize(text: str) -> str:
    client = anthropic.Anthropic()
    msg = client.messages.create(model="claude-sonnet-4-5", max_tokens=300, messages=[{"role": "user", "content": text}])
    return msg.content[0].text
