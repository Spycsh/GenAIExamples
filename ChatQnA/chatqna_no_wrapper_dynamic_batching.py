# Copyright (C) 2024 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import json
import os
import re

from comps import ChatQnAGateway, MicroService, ServiceOrchestrator, ServiceType
from langchain_core.prompts import PromptTemplate


class ChatTemplate:
    @staticmethod
    def generate_rag_prompt(question, documents):
        context_str = "\n".join(documents)
        if context_str and len(re.findall("[\u4E00-\u9FFF]", context_str)) / len(context_str) >= 0.3:
            # chinese context
            template = """
### 你将扮演一个乐于助人、尊重他人并诚实的助手，你的目标是帮助用户解答问题。有效地利用来自本地知识库的搜索结果。确保你的回答中只包含相关信息。如果你不确定问题的答案，请避免分享不准确的信息。
### 搜索结果：{context}
### 问题：{question}
### 回答：
"""
        else:
            template = """
### You are a helpful, respectful and honest assistant to help the user with questions. \
Please refer to the search results obtained from the local knowledge base. \
But be careful to not incorporate the information that you think is not relevant to the question. \
If you don't know the answer to a question, please don't share false information. \n
### Search results: {context} \n
### Question: {question} \n
### Answer:
"""
        return template.format(context=context_str, question=question)


MEGA_SERVICE_HOST_IP = os.getenv("MEGA_SERVICE_HOST_IP", "0.0.0.0")
MEGA_SERVICE_PORT = int(os.getenv("MEGA_SERVICE_PORT", 8888))
RETRIEVER_SERVICE_HOST_IP = os.getenv("RETRIEVER_SERVICE_HOST_IP", "0.0.0.0")
RETRIEVER_SERVICE_PORT = int(os.getenv("RETRIEVER_SERVICE_PORT", 7000))
# Embed/Rerank use the same host:ip, diff routers
EMBEDDING_RERANK_SERVICE_HOST_IP = os.getenv("EMBEDDING_RERANK_SERVICE_HOST_IP", "0.0.0.0")
EMBEDDING_RERANK_SERVICE_PORT = os.getenv("EMBEDDING_RERANK_SERVICE_PORT", 6001)
LLM_SERVER_HOST_IP = os.getenv("LLM_SERVER_HOST_IP", "0.0.0.0")
LLM_SERVER_PORT = int(os.getenv("LLM_SERVER_PORT", 9009))


def align_inputs(self, inputs, cur_node, runtime_graph, llm_parameters_dict, **kwargs):
    # if self.services[cur_node].service_type == ServiceType.EMBEDDING:
    #     inputs["inputs"] = inputs["text"]
    #     del inputs["text"]
    if self.services[cur_node].service_type == ServiceType.RETRIEVER:
        # prepare the retriever params
        retriever_parameters = kwargs.get("retriever_parameters", None)
        if retriever_parameters:
            inputs.update(retriever_parameters.dict())
    elif self.services[cur_node].service_type == ServiceType.RERANK:
        reranker_parameters = kwargs.get("reranker_parameters", None)
        top_n = reranker_parameters.top_n if reranker_parameters else 1
        inputs["top_n"] = top_n
    elif self.services[cur_node].service_type == ServiceType.LLM:
        prompt = inputs["query"]
        docs: list[str] = inputs["documents"]
        chat_template = llm_parameters_dict["chat_template"]
        if chat_template:
            prompt_template = PromptTemplate.from_template(chat_template)
            input_variables = prompt_template.input_variables
            if sorted(input_variables) == ["context", "question"]:
                prompt = prompt_template.format(question=prompt, context="\n".join(docs))
            elif input_variables == ["question"]:
                prompt = prompt_template.format(question=prompt)
            else:
                print(f"{prompt_template} not used, we only support 2 input variables ['question', 'context']")
                prompt = ChatTemplate.generate_rag_prompt(prompt, docs)
        else:
            prompt = ChatTemplate.generate_rag_prompt(prompt, docs)

        # inputs: LLMParamsDoc
        # {'id': 'd52f75f7bd602526073d933dab541a8c', 'model': None, 'query': 'What is the revenue of Nike in 2023?', 'max_tokens': 1024, 'max_new_tokens': 1024, 'top_k': 10, 'top_p': 0.95, 'typical_p': 0.95, 'temperature': 0.01, 'frequency_penalty': 0.0, 'presence_penalty': 0.0, 'repetition_penalty': 1.0, 'streaming': True, 'chat_template': None, 'documents': []}
        # convert TGI/vLLM to unified OpenAI /v1/chat/completions format
        next_inputs = {}
        next_inputs["model"] = "tgi"  # specifically clarify the fake model to make the format unified
        next_inputs["messages"] = [{"role": "user", "content": prompt}]
        next_inputs["max_tokens"] = llm_parameters_dict["max_new_tokens"]
        next_inputs["top_p"] = llm_parameters_dict["top_p"]
        next_inputs["stream"] = inputs["streaming"]
        next_inputs["frequency_penalty"] = inputs["repetition_penalty"]
        next_inputs["temperature"] = inputs["temperature"]
        inputs = next_inputs

    return inputs


def align_generator(self, gen, **kwargs):
    # openai reaponse format
    # b'data:{"id":"","object":"text_completion","created":1725530204,"model":"meta-llama/Meta-Llama-3-8B-Instruct","system_fingerprint":"2.0.1-native","choices":[{"index":0,"delta":{"role":"assistant","content":"?"},"logprobs":null,"finish_reason":null}]}\n\n'
    for line in gen:
        line = line.decode("utf-8")
        start = line.find("{")
        end = line.rfind("}") + 1

        json_str = line[start:end]
        try:
            # sometimes yield empty chunk, do a fallback here
            json_data = json.loads(json_str)
            if json_data["choices"][0]["finish_reason"] != "eos_token":
                yield f"data: {repr(json_data['choices'][0]['delta']['content'].encode('utf-8'))}\n\n"
        except Exception as e:
            yield f"data: {repr(json_str.encode('utf-8'))}\n\n"
    yield "data: [DONE]\n\n"


class ChatQnAService:
    def __init__(self, host="0.0.0.0", port=8000):
        self.host = host
        self.port = port
        ServiceOrchestrator.align_inputs = align_inputs
        # ServiceOrchestrator.align_outputs = align_outputs
        ServiceOrchestrator.align_generator = align_generator
        self.megaservice = ServiceOrchestrator()

    def add_remote_service(self):

        embedding = MicroService(
            name="embedding",
            host=EMBEDDING_RERANK_SERVICE_HOST_IP,
            port=EMBEDDING_RERANK_SERVICE_PORT,
            # endpoint="/embed",
            endpoint="/v1/embeddings",
            use_remote_service=True,
            service_type=ServiceType.EMBEDDING,
        )

        retriever = MicroService(
            name="retriever",
            host=RETRIEVER_SERVICE_HOST_IP,
            port=RETRIEVER_SERVICE_PORT,
            endpoint="/v1/retrieval",
            use_remote_service=True,
            service_type=ServiceType.RETRIEVER,
        )

        rerank = MicroService(
            name="rerank",
            host=EMBEDDING_RERANK_SERVICE_HOST_IP,
            port=EMBEDDING_RERANK_SERVICE_PORT,
            # endpoint="/rerank",
            endpoint="/v1/reranking",
            use_remote_service=True,
            service_type=ServiceType.RERANK,
        )

        llm = MicroService(
            name="llm",
            host=LLM_SERVER_HOST_IP,
            port=LLM_SERVER_PORT,
            endpoint="/v1/chat/completions",
            use_remote_service=True,
            service_type=ServiceType.LLM,
        )
        self.megaservice.add(embedding).add(retriever).add(rerank).add(llm)
        self.megaservice.flow_to(embedding, retriever)
        self.megaservice.flow_to(retriever, rerank)
        self.megaservice.flow_to(rerank, llm)
        self.gateway = ChatQnAGateway(megaservice=self.megaservice, host="0.0.0.0", port=self.port)


if __name__ == "__main__":
    chatqna = ChatQnAService(host=MEGA_SERVICE_HOST_IP, port=MEGA_SERVICE_PORT)
    chatqna.add_remote_service()