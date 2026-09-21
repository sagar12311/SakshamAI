
import logging
from typing import List
from chromadb.api.types import Documents, EmbeddingFunction, Embeddings
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

class SakshamEmbeddingFunction(EmbeddingFunction):
    """
    Custom embedding function using local SentenceTransformer.
    Uses 'all-MiniLM-L6-v2' (fast, small, efficient).
    """
    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        self.model_name = model_name
        try:
            self.model = SentenceTransformer(model_name)
            logger.info(f"Loaded embedding model: {model_name}")
        except Exception as e:
            logger.error(f"Failed to load embedding model: {e}")
            self.model = None

    def __call__(self, input: Documents) -> Embeddings:
        if not self.model:
            logger.warning("No embedding model loaded, returning existing/random.")
            # ChromaDB might error here, but we should have the model.
            return []
            
        try:
            embeddings = self.model.encode(input).tolist()
            return embeddings
        except Exception as e:
            logger.error(f"Embedding generation failed: {e}")
            raise e

