"""
Saksham AI - Vector Store
ChromaDB-based vector storage for semantic memory.
"""

from typing import Any, Optional
from uuid import uuid4

from loguru import logger


class VectorStore:
    """
    Vector storage using ChromaDB for semantic search.
    """
    
    def __init__(self):
        self._client = None
        self._collections: dict = {}
        
    async def initialize(self) -> None:
        """Initialize ChromaDB client"""
        try:
            import chromadb
            from chromadb.config import Settings
            from config import get_settings
            
            settings = get_settings()
            
            self._client = chromadb.PersistentClient(
                path=str(settings.chroma_persist_dir),
                settings=Settings(
                    anonymized_telemetry=False,
                ),
            )
            
            logger.info(f"ChromaDB initialized at {settings.chroma_persist_dir}")
            
        except ImportError:
            logger.warning("ChromaDB not installed, using in-memory fallback")
            self._client = None
    
    def _get_collection(self, name: str):
        """Get or create a collection"""
        if name not in self._collections:
            if self._client:
                # Use local SentenceTransformer for embeddings to avoid API dependency
                from memory.embeddings import SakshamEmbeddingFunction
                ef = SakshamEmbeddingFunction()

                try:
                    self._collections[name] = self._client.get_collection(
                        name=name,
                        embedding_function=ef,
                    )
                except Exception:
                    try:
                        # Fallback for older persisted collections whose embedding config
                        # was created outside the current code path.
                        self._collections[name] = self._client.get_collection(name=name)
                    except Exception:
                        self._collections[name] = self._client.create_collection(
                            name=name,
                            embedding_function=ef,
                            metadata={"hnsw:space": "cosine"},
                        )
            else:
                self._collections[name] = {"documents": [], "ids": [], "metadatas": []}
        return self._collections[name]

    def _list_collection_names(self) -> list[str]:
        """Return all known collections, including persisted ones."""
        if not self._client:
            return list(self._collections.keys())

        names = set(self._collections.keys())
        try:
            for collection in self._client.list_collections():
                name = getattr(collection, "name", None) or str(collection)
                if name:
                    names.add(name)
        except Exception as e:
            logger.error(f"Could not list collections: {e}")
        return sorted(names)
    
    async def store(
        self,
        content: str,
        metadata: Optional[dict] = None,
        collection: str = "general",
        doc_id: Optional[str] = None,
    ) -> str:
        """
        Store content in the vector database.
        
        Args:
            content: Text content to store
            metadata: Associated metadata
            collection: Collection name
            doc_id: Optional document ID
            
        Returns:
            The document ID
        """
        doc_id = doc_id or str(uuid4())
        coll = self._get_collection(collection)
        
        if self._client:
            coll.add(
                documents=[content],
                metadatas=[metadata or {}],
                ids=[doc_id],
            )
        else:
            # Fallback: simple in-memory storage
            coll["documents"].append(content)
            coll["ids"].append(doc_id)
            coll["metadatas"].append(metadata or {})
        
        return doc_id
    
    async def search(
        self,
        query: str,
        collection: Optional[str] = None,
        limit: int = 5,
        where: Optional[dict] = None,
    ) -> list[dict]:
        """
        Search for similar content.
        
        Args:
            query: Search query
            collection: Collection to search (None = search all)
            limit: Maximum results
            where: Metadata filter
            
        Returns:
            List of matching documents with scores
        """
        results = []
        
        collections_to_search = (
            [collection] if collection 
            else self._list_collection_names()
        )
        
        for coll_name in collections_to_search:
            coll = self._get_collection(coll_name)
            
            if self._client:
                try:
                    result = coll.query(
                        query_texts=[query],
                        n_results=limit,
                        where=where,
                    )
                    
                    for i, doc in enumerate(result.get("documents", [[]])[0]):
                        results.append({
                            "content": doc,
                            "metadata": result["metadatas"][0][i] if result.get("metadatas") else {},
                            "id": result["ids"][0][i] if result.get("ids") else None,
                            "distance": result["distances"][0][i] if result.get("distances") else None,
                            "collection": coll_name,
                        })
                except Exception as e:
                    logger.error(f"Search error in {coll_name}: {e}")
            else:
                # Fallback: simple string matching
                for i, doc in enumerate(coll.get("documents", [])):
                    if query.lower() in doc.lower():
                        results.append({
                            "content": doc,
                            "metadata": coll["metadatas"][i],
                            "id": coll["ids"][i],
                            "collection": coll_name,
                        })
        
        return results[:limit]

    async def list_documents(
        self,
        collection: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict]:
        """List stored documents without semantic search."""
        results = []
        collections_to_list = [collection] if collection else self._list_collection_names()

        for coll_name in collections_to_list:
            coll = self._get_collection(coll_name)

            if self._client:
                try:
                    result = coll.get(limit=limit, include=["documents", "metadatas"])
                    documents = result.get("documents", [])
                    metadatas = result.get("metadatas", [])
                    ids = result.get("ids", [])

                    for index, document in enumerate(documents):
                        results.append({
                            "content": document,
                            "metadata": metadatas[index] if index < len(metadatas) else {},
                            "id": ids[index] if index < len(ids) else None,
                            "collection": coll_name,
                        })
                except Exception as e:
                    logger.error(f"List error in {coll_name}: {e}")
            else:
                for index, document in enumerate(coll.get("documents", [])):
                    results.append({
                        "content": document,
                        "metadata": coll["metadatas"][index],
                        "id": coll["ids"][index],
                        "collection": coll_name,
                    })

        return results[:limit]
    
    async def delete(
        self,
        doc_id: str,
        collection: str = "general",
    ) -> bool:
        """Delete a document by ID"""
        coll = self._get_collection(collection)
        
        if self._client:
            try:
                coll.delete(ids=[doc_id])
                return True
            except Exception as e:
                logger.error(f"Delete error: {e}")
                return False
        else:
            try:
                idx = coll["ids"].index(doc_id)
                coll["documents"].pop(idx)
                coll["ids"].pop(idx)
                coll["metadatas"].pop(idx)
                return True
            except ValueError:
                return False

    async def delete_document(
        self,
        doc_id: str,
        collection: Optional[str] = None,
    ) -> bool:
        """Delete a document, optionally searching all collections."""
        collections_to_check = [collection] if collection else self._list_collection_names()

        for coll_name in collections_to_check:
            coll = self._get_collection(coll_name)

            if self._client:
                try:
                    result = coll.get(ids=[doc_id], include=["metadatas"])
                    if result.get("ids"):
                        coll.delete(ids=[doc_id])
                        return True
                except Exception as e:
                    logger.error(f"Delete lookup error in {coll_name}: {e}")
            else:
                if doc_id in coll.get("ids", []):
                    return await self.delete(doc_id, collection=coll_name)

        return False
    
    async def update(
        self,
        doc_id: str,
        content: str,
        metadata: Optional[dict] = None,
        collection: str = "general",
    ) -> bool:
        """Update a document"""
        coll = self._get_collection(collection)
        
        if self._client:
            try:
                coll.update(
                    ids=[doc_id],
                    documents=[content],
                    metadatas=[metadata or {}],
                )
                return True
            except Exception as e:
                logger.error(f"Update error: {e}")
                return False
        else:
            try:
                idx = coll["ids"].index(doc_id)
                coll["documents"][idx] = content
                coll["metadatas"][idx] = metadata or {}
                return True
            except ValueError:
                return False
    
    async def get_stats(self) -> dict:
        """Get storage statistics"""
        stats = {}
        for name in self._list_collection_names():
            coll = self._get_collection(name)
            if self._client:
                stats[name] = coll.count()
            else:
                stats[name] = len(coll.get("documents", []))
        return stats
