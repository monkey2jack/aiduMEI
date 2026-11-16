# Local OpenAI-compatible embeddings shim for aiduMEI native kernel.
# Serves BAAI/bge-small-zh-v1.5 (512d) via fastembed on 127.0.0.1:8770.
import os
from fastapi import FastAPI
from pydantic import BaseModel
from typing import Union, List

app = FastAPI()
_model = None

def get_model():
    global _model
    if _model is None:
        from fastembed import TextEmbedding
        cache = os.path.expanduser('~/.cache/aidumei/fastembed')
        _model = TextEmbedding(model_name='BAAI/bge-small-zh-v1.5', cache_dir=cache)
    return _model

class EmbReq(BaseModel):
    input: Union[str, List[str]]
    model: str = 'BAAI/bge-small-zh-v1.5'

@app.post('/v1/embeddings')
def embeddings(req: EmbReq):
    texts = [req.input] if isinstance(req.input, str) else list(req.input)
    vecs = list(get_model().embed(texts))
    data = [{'object': 'embedding', 'index': i, 'embedding': [float(x) for x in v]} for i, v in enumerate(vecs)]
    return {'object': 'list', 'data': data, 'model': req.model, 'usage': {'prompt_tokens': 0, 'total_tokens': 0}}

@app.get('/health')
def health():
    return {'status': 'ok'}

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=8770, log_level='warning')
