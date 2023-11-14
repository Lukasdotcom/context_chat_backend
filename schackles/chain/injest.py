from logging import error as log_error

from fastapi.datastructures import UploadFile
from langchain.schema import Document
from langchain.text_splitter import (
	MarkdownTextSplitter,
	RecursiveCharacterTextSplitter,
	TextSplitter
)

from ..utils import to_int
from ..vectordb import BaseVectorDB


_ALLOWED_MIME_TYPES = [
	'text/plain',
	'text/markdown',
	'application/json',
]


def _allowed_file(file: UploadFile) -> bool:
	return file.headers.get('type', default='') in _ALLOWED_MIME_TYPES


def _get_splitter_for(mimetype: str = "text/plain") -> TextSplitter:
	kwargs = {
		"chunk_size": 3000,
		"chunk_overlap": 200,
		"add_start_index": True,
		"strip_whitespace": True,
		"is_separator_regex": True,
	}

	if mimetype == "text/plain" or not mimetype:
		return RecursiveCharacterTextSplitter(separators=["\n\n", "\n", ".", " ", ""], **kwargs)

	if mimetype == "text/markdown":
		return MarkdownTextSplitter(**kwargs)

	if mimetype == "application/json":
		return RecursiveCharacterTextSplitter(separators=["{", "}", "[", "]", ",", ""], **kwargs)


def _filter_documents(
	user_id: str,
	vectordb: BaseVectorDB,
	documents: list[Document]
) -> list[Document]:
	"""
	Returns a filtered list of documents that are not already in the vectordb
	or have been modified since they were last added.
	It also deletes the old documents to prevent duplicates.
	"""
	to_delete = {}

	input_sources = {}
	for meta in documents:
		if meta.metadata.get("source") is None:
			continue
		input_sources[meta.metadata.get("source")] = meta.metadata.get("modified")

	existing_objects = vectordb.get_objects_from_sources(user_id, list(input_sources.keys()))
	for source, existing_meta in existing_objects.items():
		# recently modified files are re-embedded
		if to_int(input_sources.get(source)) > to_int(existing_meta.get("modified")):
			to_delete[source] = existing_meta.get("id")

	# delete old sources
	vectordb.delete_by_ids(user_id, to_delete.values())

	# sources not already in the vectordb + the ones that were deleted
	new_sources = set(input_sources.keys()) \
		.difference(set(existing_objects))
	new_sources.update(set(to_delete.keys()))

	filtered_documents = [
		doc for doc in documents
		if doc.metadata.get("source") in new_sources
	]

	return filtered_documents


def _sources_to_documents(sources: list[UploadFile]) -> list[Document]:
	documents = {}

	for source in sources:
		user_id = source.headers.get("userId")
		if user_id is None:
			log_error("userId not found in headers for source: " + source.filename)
			continue

		content = source.file.read().decode("utf-8")
		metadata = {
			"source": source.filename,
			"type": source.headers.get("type"),
			"modified": source.headers.get("modified"),
		}

		document = Document(page_content=content, metadata=metadata)

		if documents.get(user_id) is not None:
			documents[user_id].append(document)
		else:
			documents[user_id] = [document]

	return documents


def _bucket_by_type(documents: list[Document]) -> dict[str, list[Document]]:
	bucketed_documents = {}

	for doc in documents:
		doc_type = doc.metadata.get("type")

		if bucketed_documents.get(doc_type) is not None:
			bucketed_documents[doc_type].append(doc)
		else:
			bucketed_documents[doc_type] = [doc]

	return bucketed_documents


def _process_sources(vectordb: BaseVectorDB, sources: list[UploadFile]) -> bool:
	ddocuments: dict[str, list[Document]] = _sources_to_documents(sources)

	if len(ddocuments.keys()) == 0:
		return []

	success = True

	for user_id, documents in ddocuments.items():
		split_documents: list[Document] = []
		filtered_docs = _filter_documents(user_id, vectordb, documents)

		if len(filtered_docs) == 0:
			continue

		type_bucketed_docs = _bucket_by_type(filtered_docs)

		for _type, _docs in type_bucketed_docs.items():
			text_splitter = _get_splitter_for(_type)
			split_docs = text_splitter.split_documents(_docs)
			split_documents.extend(split_docs)

		# filter out empty documents
		split_documents = list(filter(lambda doc: doc.page_content != "", split_documents))
		if len(split_documents) == 0:
			continue

		user_client = vectordb.get_user_client(user_id)
		if user_client is None:
			log_error('Error: Weaviate client not initialised')
			return {}

		doc_ids = user_client.add_documents(split_documents)

		# does not do per document error checking
		success &= len(split_documents) == len(doc_ids)

	return success


def embed_sources(
	vectordb: BaseVectorDB,
	sources: list[UploadFile],
) -> bool:
	# either not a file or a file that is allowed
	sources_filtered = [
		source for source in sources
		if not source.filename.startswith("file: ")
		or _allowed_file(source)
	]

	return _process_sources(vectordb, sources_filtered)
