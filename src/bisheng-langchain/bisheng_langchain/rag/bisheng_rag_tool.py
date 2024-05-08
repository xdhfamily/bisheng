import time
import os
import yaml
import httpx
from typing import Any, Dict, Tuple, Type, Union, Optional
from loguru import logger
from langchain_core.tools import BaseTool, Tool
from langchain_core.pydantic_v1 import BaseModel, Extra, Field, root_validator
from langchain_core.language_models.base import LanguageModelLike
from langchain.chains.question_answering import load_qa_chain
from bisheng_langchain.retrievers import EnsembleRetriever
from bisheng_langchain.vectorstores import ElasticKeywordsSearch, Milvus
from bisheng_langchain.rag.init_retrievers import (
    BaselineVectorRetriever,
    KeywordRetriever,
    MixRetriever,
    SmallerChunksVectorRetriever,
)
from bisheng_langchain.rag.utils import import_by_type, import_class
from bisheng_langchain.rag.extract_info import extract_title


class MultArgsSchemaTool(Tool):

    def _to_args_and_kwargs(self, tool_input: Union[str, Dict]) -> Tuple[Tuple, Dict]:
        # For backwards compatibility, if run_input is a string,
        # pass as a positional argument.
        if isinstance(tool_input, str):
            return (tool_input, ), {}
        else:
            return (), tool_input


class BishengRAGTool:

    def __init__(
        self,
        vector_store: Optional[Milvus] = None,
        keyword_store: Optional[ElasticKeywordsSearch] = None,
        llm: Optional[LanguageModelLike] = None,
        collection_name: Optional[str] = None,
        **kwargs
    ) -> None:
        if collection_name is None and (keyword_store is None or vector_store is None):
            raise ValueError('collection_name must be provided if keyword_store or vector_store is not provided')
        self.collection_name = collection_name
        
        yaml_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config/baseline_v2.yaml')
        with open(yaml_path, 'r') as f:
            self.params = yaml.safe_load(f)
        
        # init milvus
        if vector_store:
            self.vector_store = vector_store
        else:
            # init embeddings
            embedding_params = self.params['embedding']
            embedding_object = import_by_type(_type='embeddings', name=embedding_params['type'])
            if embedding_params['type'] == 'OpenAIEmbeddings' and embedding_params['openai_proxy']:
                embedding_params.pop('type')
                self.embeddings = embedding_object(
                    http_client=httpx.Client(proxies=embedding_params['openai_proxy']), **embedding_params
                )
            else:
                embedding_params.pop('type')
                self.embeddings = embedding_object(**embedding_params)
            
            self.vector_store = Milvus(
                embedding_function=self.embeddings,
                connection_args={
                    "host": self.params['milvus']['host'],
                    "port": self.params['milvus']['port'],
                },
            )
        
        # init keyword store
        if keyword_store:
            self.keyword_store = keyword_store
        else:
            self.keyword_store = ElasticKeywordsSearch(
                index_name='default_es',
                elasticsearch_url=self.params['elasticsearch']['url'],
                ssl_verify=self.params['elasticsearch']['ssl_verify'],
            )
        
        # init llm
        if llm:
            self.llm = llm
        else:
            llm_params = self.params['chat_llm']
            llm_object = import_by_type(_type='llms', name=llm_params['type'])
            if llm_params['type'] == 'ChatOpenAI' and llm_params['openai_proxy']:
                llm_params.pop('type')
                self.llm = llm_object(http_client=httpx.Client(proxies=llm_params['openai_proxy']), **llm_params)
            else:
                llm_params.pop('type')
                self.llm = llm_object(**llm_params)

        # init retriever
        retriever_list = []
        retrievers = self.params['retriever']['retrievers']
        for retriever in retrievers:
            retriever_type = retriever.pop('type')
            retriever_params = {
                'vector_store': self.vector_store,
                'keyword_store': self.keyword_store,
                'splitter_kwargs': retriever['splitter'],
                'retrieval_kwargs': retriever['retrieval'],
            }
            retriever_list.append(self._post_init_retriever(retriever_type=retriever_type, **retriever_params))
        self.retriever = EnsembleRetriever(retrievers=retriever_list)

        # init qa chain    
        if 'prompt_type' in self.params['generate']:
            prompt_type = self.params['generate']['prompt_type']
            prompt = import_class(f'bisheng_langchain.rag.prompts.{prompt_type}')
        else:
            prompt = None
        self.qa_chain = load_qa_chain(
            llm=self.llm, 
            chain_type=self.params['generate']['chain_type'], 
            prompt=prompt, 
            verbose=False
        )
    
    def _post_init_retriever(self, retriever_type, **kwargs):
        retriever_classes = {
            'KeywordRetriever': KeywordRetriever,
            'BaselineVectorRetriever': BaselineVectorRetriever,
            'MixRetriever': MixRetriever,
            'SmallerChunksVectorRetriever': SmallerChunksVectorRetriever,
        }
        if retriever_type not in retriever_classes:
            raise ValueError(f'Unknown retriever type: {retriever_type}')

        input_kwargs = {}
        splitter_params = kwargs.pop('splitter_kwargs')
        for key, value in splitter_params.items():
            splitter_obj = import_by_type(_type='textsplitters', name=value.pop('type'))
            input_kwargs[key] = splitter_obj(**value)

        retrieval_params = kwargs.pop('retrieval_kwargs')
        for key, value in retrieval_params.items():
            input_kwargs[key] = value

        input_kwargs['vector_store'] = kwargs.pop('vector_store')
        input_kwargs['keyword_store'] = kwargs.pop('keyword_store')

        retriever_class = retriever_classes[retriever_type]
        return retriever_class(**input_kwargs)

    def file2knowledge(self, file_path, drop_old=True):
        """
        file to knowledge
        """
        loader_params = self.params['loader']
        loader_object = import_by_type(_type='documentloaders', name=loader_params.pop('type'))

        logger.info(f'file_path: {file_path}')
        loader = loader_object(
            file_name=os.path.basename(file_path), file_path=file_path, **loader_params
        )
        documents = loader.load()
        logger.info(f'documents: {len(documents)}, page_content: {len(documents[0].page_content)}')
        if len(documents[0].page_content) == 0:
            logger.error(f'{file_path} page_content is empty.')

        # add aux info
        add_aux_info = self.params['retriever'].get('add_aux_info', False)
        if add_aux_info:
            for doc in documents:
                try:
                    title = extract_title(llm=self.llm, text=doc.page_content)
                    logger.info(f'extract title: {title}')
                except Exception as e:
                    logger.error(f"Failed to extract title: {e}")
                    title = ''
                doc.metadata['title'] = title

        for idx, retriever in enumerate(self.retriever.retrievers):
            retriever.add_documents(
                documents, 
                self.collection_name, 
                drop_old=drop_old, 
                add_aux_info=add_aux_info
            )
    
    def retrieval_and_rerank(self, query):
        """
        retrieval and rerank
        """
        # EnsembleRetriever直接检索召回会默认去重
        docs = self.retriever.get_relevant_documents(
            query=query, 
            collection_name=self.collection_name
        )
        logger.info(f'retrieval docs origin: {len(docs)}')

        # delete redundancy according to max_content 
        doc_num, doc_content_sum = 0, 0
        for doc in docs:
            doc_content_sum += len(doc.page_content)
            if doc_content_sum > self.params['generate']['max_content']:
                break
            doc_num += 1
        docs = docs[:doc_num]
        logger.info(f'retrieval docs after delete redundancy: {len(docs)}')

        # 按照文档的source和chunk_index排序，保证上下文的连贯性和一致性
        if self.params['post_retrieval'].get('sort_by_source_and_index', False):
            logger.info('sort chunks by source and chunk_index')
            docs = sorted(docs, key=lambda x: (x.metadata['source'], x.metadata['chunk_index']))
        return docs
    
    def run(self, query) -> str:
        docs = self.retrieval_and_rerank(query)
        try:
            ans = self.qa_chain({"input_documents": docs, "question": query}, return_only_outputs=True)
        except Exception as e:
            logger.error(f'question: {query}\nerror: {e}')
            ans = {'output_text': str(e)}
        rag_answer = ans['output_text']
        return rag_answer
    
    async def arun(self, query: str) -> str:
        rag_answer = self.run(query)
        return rag_answer
    
    @classmethod
    def get_rag_tool(cls, name, description, **kwargs: Any) -> BaseTool:
        class InputArgs(BaseModel):
            query: str = Field(description='question asked by the user.')

        return MultArgsSchemaTool(name=name,
                                  description=description,
                                  func=cls(**kwargs).run,
                                  coroutine=cls(**kwargs).arun,
                                  args_schema=InputArgs)
    

if __name__ == '__main__':
    # rag_tool = BishengRAGTool(collection_name='rag_finance_report_0_test')
    # rag_tool.file2knowledge(file_path='/home/public/rag_benchmark_finance_report/金融年报财报的来源文件/2021-04-23__金宇生物技术股份有限公司__600201__生物股份__2020年__年度报告.pdf')

    from langchain.chat_models import ChatOpenAI
    from langchain.embeddings import OpenAIEmbeddings
    # embedding
    embeddings = OpenAIEmbeddings(model='text-embedding-ada-002')
    # llm
    llm = ChatOpenAI(model='gpt-4-1106-preview', temperature=0.01)
    collection_name = 'rag_finance_report_0_benchmark_caibao_1000_source_title'
    # milvus
    vector_store = Milvus(
            collection_name=collection_name,
            embedding_function=embeddings,
            connection_args={
                "host": '110.16.193.170',
                "port": '50062',
            },
    )
    # es
    keyword_store = ElasticKeywordsSearch(
        index_name=collection_name,
        elasticsearch_url='http://110.16.193.170:50062/es',
        ssl_verify={'basic_auth': ["elastic", "oSGL-zVvZ5P3Tm7qkDLC"]},
    )

    tool = BishengRAGTool.get_rag_tool(
        name='rag_knowledge_retrieve', 
        description='金融年报财报知识库问答',
        vector_store=vector_store, 
        keyword_store=keyword_store, 
        llm=llm
    )
    print(tool.run('能否根据2020年金宇生物技术股份有限公司的年报，给我简要介绍一下报告期内公司的社会责任工作情况？'))

    # tool = BishengRAGTool.get_rag_tool(
    #     name='rag_knowledge_retrieve', 
    #     description='金融年报财报知识库问答',
    #     collection_name='rag_finance_report_0_benchmark_caibao_1000_source_title'
    # )
    # print(tool.run('能否根据2020年金宇生物技术股份有限公司的年报，给我简要介绍一下报告期内公司的社会责任工作情况？'))