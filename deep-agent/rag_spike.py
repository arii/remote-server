"""RAG spike: index repo docs + Pi skills, retrieve, answer grounded."""

import glob
import os
import sys

from dotenv import load_dotenv
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_ollama import OllamaEmbeddings
from langchain_openai import ChatOpenAI
from langchain_text_splitters import RecursiveCharacterTextSplitter

ROOT = "/srv/remote-server"
DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chroma_db")
SOURCES = [
    "docs/*.md",
    "terminal/pi-config/skills/*/*.md",
    "terminal/pi-config/AGENTS.md",
    "terminal/pi-config/APPEND_SYSTEM.md",
    "AGENTS.md",
]
QUERY = "What td commands is Pi allowed to suggest, and what is the approval flow?"


def load_docs() -> list[Document]:
    docs = []
    for pattern in SOURCES:
        for path in sorted(glob.glob(os.path.join(ROOT, pattern))):
            with open(path, encoding="utf-8") as f:
                docs.append(
                    Document(
                        page_content=f.read(),
                        metadata={
                            "source": os.path.relpath(path, ROOT),
                            "repo": "remote-server",
                        },
                    )
                )
    return docs


def main() -> int:
    load_dotenv(os.path.join(ROOT, ".env"))
    if not os.environ.get("NEBIUS_API_KEY"):
        print("NEBIUS_API_KEY is not set", file=sys.stderr)
        return 1
    docs = load_docs()
    print(f"loaded {len(docs)} documents")
    splits = RecursiveCharacterTextSplitter(
        chunk_size=1000, chunk_overlap=200
    ).split_documents(docs)
    print(f"split into {len(splits)} chunks")
    embeddings = OllamaEmbeddings(
        model="nomic-embed-text", base_url="http://ollama:11434"
    )
    Chroma.from_documents(
        documents=splits,
        embedding=embeddings,
        persist_directory=DB_DIR,
        collection_name="s5-docs",
    )
    print(f"indexed into {DB_DIR}")
    vectorstore = Chroma(
        persist_directory=DB_DIR,
        embedding_function=embeddings,
        collection_name="s5-docs",
    )
    hits = vectorstore.as_retriever(search_kwargs={"k": 4}).invoke(QUERY)
    print("retrieved:", [h.metadata.get("source") for h in hits])
    context = "\n\n".join(h.page_content for h in hits)
    model = ChatOpenAI(
        model="zai-org/GLM-5.3-Flash",
        base_url="https://api.studio.nebius.com/v1",
        api_key=os.environ["NEBIUS_API_KEY"],
        temperature=0,
        max_tokens=2000,
    )
    resp = model.invoke(
        [
            {
                "role": "system",
                "content": (
                    "Answer ONLY from the context below. If the answer "
                    f"is not there, say so.\n\n{context}"
                ),
            },
            {"role": "user", "content": QUERY},
        ]
    )
    print("ANSWER:", resp.content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
