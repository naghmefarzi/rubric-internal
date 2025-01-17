import torch as pt
import pandas as pd
import numpy as np
from typing import NewType, List, Optional, Dict, Any
from pathlib import Path
import torch.nn.functional as F
from enum import Enum, auto

import duckdb

VectorId = NewType('VectorId', int)
ClassificationItemId = NewType('ClassificationItemId', int)


class Align(Enum):
    ALIGN_END = auto()
    ALIGN_BEGIN = auto()

SCHEMA = '''
CREATE SEQUENCE tensor_storage_id_seq START 1;
CREATE SEQUENCE tensor_id_seq START 1;
CREATE TABLE tensor (
    tensor_id INTEGER PRIMARY KEY DEFAULT nextval('tensor_id_seq'),
    tensor_storage_id integer,
    index_n integer NOT NULL,   -- index within backing store
);

CREATE SEQUENCE classification_item_id_seq START 1;
CREATE TABLE classification_item (
    classification_item_id INTEGER PRIMARY KEY DEFAULT nextval('classification_item_id_seq'),
    metadata JSON,
);

CREATE SEQUENCE classification_feature_id_seq START 1;
CREATE TABLE classification_feature (
    classification_feature_id INTEGER PRIMARY KEY DEFAULT nextval('classification_feature_id_seq'),
    classification_item_id integer references classification_item(classification_item_id),
    tensor_id integer references tensor(tensor_id),
    metadata JSON,
);

CREATE SEQUENCE label_assignment_id_seq START 1;
CREATE TABLE label_assignment (
    label_assignment_id INTEGER PRIMARY KEY DEFAULT nextval('label_assignment_id_seq'),
    classification_item_id integer references classification_item(classification_item_id),
    true_labels text[],
);
'''

class EmbeddingDb:
    def __init__(self, path: Path):
        self.tensor_dir = path / "tensors"
        needs_init = not path.exists()
        self.tensor_dir.mkdir(parents=True, exist_ok=True)
        self.db = duckdb.connect(path / 'embeddings.duckdb')
        if needs_init:
            self.db.execute(SCHEMA)
        self.storage_cache:Dict[Any,Any] = dict()


    def _storage_path(self, tsid: int) -> Path:
        return self.tensor_dir / f'{tsid:08x}.pt'


    def _insert_tensors(self, tensors: pt.Tensor) -> List[VectorId]:
        self.db.begin()
        self.db.execute("SELECT nextval('tensor_storage_id_seq');")
        tsid, = self.db.fetchone()
        path = self._storage_path(tsid)
        try:
            pt.save(tensors, path)
            df = pd.DataFrame(data={
                'index_n': np.arange(tensors.shape[0]),
                'tensor_storage_id': tsid,
            })
            self.db.execute(
                    '''
                    INSERT INTO tensor (tensor_storage_id, index_n)
                    (SELECT tensor_storage_id, index_n FROM df)
                    RETURNING tensor_id;
                    ''')
            tensor_id0, = self.db.fetchone()
            self.db.commit()
            tensor_ids = list(range(tensor_id0, tensor_id0 + tensors.shape[0]))
            return tensor_ids
        except Exception as e:
            path.unlink()
            self.db.rollback()
            raise e

    @staticmethod
    def cat_cut_pad_dim1_tensors(tensors: list[pt.Tensor], dim_len:Optional[int])->pt.Tensor:
        dim = 1  # if you want to affect other dimensions, then some pieces of the code below need to change also

        max_dim1 = max(tensor.size(dim) for tensor in tensors)
        min_dim1 = min(tensor.size(dim) for tensor in tensors)

        if max_dim1 == min_dim1 and (dim_len is None or max_dim1 == dim_len):
                # all tensors have desired length
                return pt.cat(tensors, dim=dim)
        else:
            adjusted_tensors = list()
            for tensor in tensors:
                if tensor.size(dim) < dim_len:  
                    # Pad if shorter
                    adjusted_tensors.append(F.pad(tensor, (0, 0, 0, dim_len - tensor.size(dim))))
                else:  
                    # Chop if longer
                    adjusted_tensors.append(tensor[:, :dim_len, :])
            return pt.cat(adjusted_tensors, dim=dim)


    
    def fetch_tensors(self, tensor_ids: List[VectorId], token_length:Optional[int]=None, align:Align=Align.ALIGN_BEGIN) -> pt.Tensor:
        tensor_ids_df = pd.DataFrame(data={'tensor_id': tensor_ids})
        tensor_ids_df['i'] = tensor_ids_df.index
        self.db.execute('''
            SELECT needles.i, tensor_storage_id, index_n
            FROM tensor
            INNER JOIN (SELECT * FROM tensor_ids_df) AS needles ON tensor.tensor_id = needles.tensor_id
            ORDER BY needles.i ASC;
        ''')
        vs = self.db.df()
        out = None
        out_shape=None
        for v in vs.itertuples():
            tsid = v.tensor_storage_id
            if tsid not in self.storage_cache:
                self.storage_cache[tsid] = pt.load(self._storage_path(tsid), weights_only=True)

            t:pt.Tensor = (self.storage_cache[tsid])[v.index_n]  # [tok_len, d_model]

            batch_sz = len(tensor_ids)
            token_len = token_length or t.shape[0]
            model_dim = t.shape[1]
            if out is None:
                out_shape = (batch_sz, token_len, model_dim)
                out = pt.zeros(size=out_shape, dtype=pt.float)

            take_tokens = min(token_len, t.shape[0])
            assert(t.shape[1]==out_shape[2])
            if align == Align.ALIGN_BEGIN:
                out[v.i,0:take_tokens,:] = t[0:take_tokens,:]
            elif align == Align.ALIGN_END:
                out[v.i, -take_tokens:, :] = t[-take_tokens:, :]

        self.storage_cache = dict()

        assert out is not None
        return out


    def add_embeddings(self,
                       # entries marked with "# <-" mark the set that denotes a unique entry.
            query_id: str, # <-
            passage_id: str, # <-
            prompt_class: str, # <- 
            prompt_texts: List[str],
            test_bank_ids: List[str], # <-
            answers: List[str],
            embeddings: pt.Tensor,
            true_labels: Optional[List[str]]
            ):
        #raise RuntimeError('uh oh') # XXX
        print(f"Recording {query_id} {passage_id} embeddings:{embeddings.shape}")
        tensor_ids = self._insert_tensors(embeddings)

        metadata = {
            'query': query_id,
            'passage': passage_id,
        }
        self.db.execute(
            '''
            INSERT INTO classification_item (metadata)
            VALUES (?)
            RETURNING classification_item_id;
            ''',
            (metadata,))
        ci_id, = self.db.fetchone()
        # print('hello', ci_id, metadata)

        self.db.executemany(
            '''
            INSERT INTO classification_feature
            (classification_item_id, tensor_id, metadata)
            VALUES (?, ?, ?);
            ''',
            [(ci_id, tid, {
                'text': text,
                'prompt_class': prompt_class,
                'test_bank': test_bank,
                'answer': answer,
              })
             for tid, text, test_bank, answer in zip(tensor_ids, prompt_texts, test_bank_ids, answers)
            ])

        if true_labels is not None:
            self.db.execute(
                '''
                INSERT INTO label_assignment
                (classification_item_id, true_labels)
                VALUES (?,?)
                ''',
                (ci_id, true_labels))


def main() -> None:
    p = Path('test.vecdb')
    dims = (512,1024)
    N = 64

    db = EmbeddingDb(p)
    accum = []
    for i in range(8):
        t = pt.rand((N,) + dims)
        t[:,:,:] = pt.arange(N)[:,None,None] + i*N
        print(t.shape)
        tensor_ids = db._insert_tensors(t)
        print(tensor_ids)
        accum += tensor_ids

    xs = db.fetch_tensors(accum)
    print(xs)

if __name__ == '__main__':
    main()
