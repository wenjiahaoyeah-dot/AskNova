"""In-memory BM25 and reciprocal rank fusion; ChromaDB remains the source of truth."""
import math
import re
from collections import Counter, defaultdict


def tokenize(text):
    """保留英文词/错误码；中文使用单字和双字片段，不依赖额外分词模型。"""
    tokens = []
    for match in re.finditer(r"[a-z0-9]+(?:[_.-][a-z0-9]+)*|[\u3400-\u4dbf\u4e00-\u9fff]+", text.casefold()):
        word = match.group()
        if '\u3400' <= word[0] <= '\u9fff':
            tokens.extend(word)
            tokens.extend(word[i:i + 2] for i in range(len(word) - 1))
        else:
            tokens.append(word)
    return tokens


class BM25Index:
    """Immutable chunk snapshot with an inverted index (BM25 k1=1.5, b=0.75)."""

    def __init__(self, rows):
        self.rows = rows
        self.postings = defaultdict(dict)
        self.lengths = {}
        for chunk_id, row in rows.items():
            terms = Counter(tokenize(row.get('title', '') + '\n' + row['content']))
            self.lengths[chunk_id] = sum(terms.values())
            for term, frequency in terms.items():
                self.postings[term][chunk_id] = frequency
        self.average_length = sum(self.lengths.values()) / len(rows) if rows else 0

    def search(self, query, top_k):
        scores = defaultdict(float)
        n = len(self.rows)
        if not n or not self.average_length:
            return []
        for term in sorted(set(tokenize(query))):
            postings = self.postings.get(term, {})
            if not postings:
                continue
            idf = math.log(1 + (n - len(postings) + 0.5) / (len(postings) + 0.5))
            for chunk_id, frequency in postings.items():
                norm = 1.5 * (1 - 0.75 + 0.75 * self.lengths[chunk_id] / self.average_length)
                scores[chunk_id] += idf * frequency * 2.5 / (frequency + norm)
        ranked = sorted(scores, key=lambda key: (-scores[key], key))[:top_k]
        return [dict(self.rows[key], bm25_score=scores[key]) for key in ranked]


def fuse_rankings(rankings, top_k, rank_constant=60):
    """Equal-weight RRF: sum(1/(60+rank)); scores are ranks, not probabilities."""
    merged = {}
    for source, items in rankings.items():
        seen = set()
        for rank, item in enumerate(items, 1):
            key = item['chunk_id']
            if key in seen:
                continue
            seen.add(key)
            row = merged.setdefault(key, dict(item, retrieval_sources=[], rrf_score=0.0))
            row['retrieval_sources'].append(source)
            row['rrf_score'] += 1.0 / (rank_constant + rank)
            for field in ('vector_score', 'bm25_score'):
                if field in item:
                    row[field] = item[field]
    result = sorted(merged.values(), key=lambda row: (-row['rrf_score'], row['chunk_id']))[:top_k]
    for row in result:
        row['score'] = row['rrf_score']
    return result
