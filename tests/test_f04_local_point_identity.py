"""向量点身份遵循真实 Qdrant 类型，事实键不能污染整批删除。"""
import uuid

import pytest
from qdrant_client import QdrantClient, models

from ducky.dual_index import LOCAL_COLLECTION, delete_local


@pytest.fixture
def client():
    client = QdrantClient(':memory:')
    client.create_collection(LOCAL_COLLECTION, vectors_config=models.VectorParams(size=2, distance=models.Distance.COSINE))
    yield client
    client.close()


def put(client, point_id, owner='alice', bank='work'):
    client.upsert(LOCAL_COLLECTION, [models.PointStruct(
        id=point_id, vector=[.1, .2], payload={'user_id': owner, 'bank_id': bank})], wait=True)


@pytest.mark.parametrize('point_id', [0, 42, 2**64-1, str(uuid.UUID(int=17))])
def test_valid_points_delete_with_exact_scope_and_original_wire_type(client, point_id):
    put(client, point_id)
    foreign = str(uuid.UUID(int=18))
    put(client, foreign, owner='bob')
    # HTTP 的 memory_id 是文本；整数点必须转回整数而非字符串送后端。
    assert delete_local(['structured-key', str(point_id), foreign, str(point_id)],
                        client=client, user_id='alice', bank_id='work') == 1
    assert client.retrieve(LOCAL_COLLECTION, [point_id]) == []
    assert len(client.retrieve(LOCAL_COLLECTION, [foreign])) == 1


def test_structured_keys_do_not_contact_point_backend(monkeypatch):
    import ducky.dual_index as di
    def unexpected():
        raise AssertionError('事实键没有同名 Qdrant 点，不应发无效请求')
    monkeypatch.setattr(di, '_qdrant_client', unexpected)
    assert delete_local(['f04-delete-synthetic', 'fact:7', 'raw:abc']) == 0


@pytest.mark.parametrize('operation', ['get_collections', 'retrieve', 'delete'])
def test_real_point_failures_are_never_treated_as_absence(client, monkeypatch, operation):
    point_id = str(uuid.UUID(int=19))
    put(client, point_id)
    def unavailable(*args, **kwargs):
        raise OSError('isolated backend fault')
    with monkeypatch.context() as patch:
        patch.setattr(client, operation, unavailable)
        with pytest.raises(OSError, match='isolated backend fault'):
            delete_local(['structured-key', point_id], client=client, user_id='alice', bank_id='work')
    assert len(client.retrieve(LOCAL_COLLECTION, [point_id])) == 1
