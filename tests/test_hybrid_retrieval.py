"""Offline retrieval regressions; no model downloads, API calls or database needed."""
import asyncio
import threading
import unittest
from unittest.mock import patch

from test_knowledge_base import make_kb
from tooling.hybrid_retrieval import BM25Index, fuse_rankings, tokenize


def row(key, content, title=''):
    return dict(chunk_id=key, content=content, title=title, chunk=0,
                document_id=key, version='v1')


class HybridRetrievalTests(unittest.TestCase):
    def test_chinese_bigrams_and_exact_error_codes(self):
        tokens = tokenize('退款政策 API E_AUTH_401 v2.0')
        self.assertIn('退款', tokens)
        self.assertIn('政策', tokens)
        self.assertIn('api', tokens)
        self.assertIn('e_auth_401', tokens)
        self.assertIn('v2.0', tokens)

    def test_bm25_matches_chinese_and_does_not_return_unmatched_rows(self):
        index = BM25Index({'refund': row('refund', '七天内申请退款'),
                           'login': row('login', '检查登录密码')})
        self.assertEqual([r['chunk_id'] for r in index.search('退款', 5)], ['refund'])
        self.assertEqual(index.search('不存在的英文term', 5), [])

    def test_rare_error_code_and_title_are_searchable(self):
        index = BM25Index({'error': row('error', '权限配置', 'E_AUTH_401'),
                           'other': row('other', '正常说明')})
        self.assertEqual(index.search('e_auth_401', 1)[0]['chunk_id'], 'error')

    def test_empty_documents_and_empty_query_tokens(self):
        self.assertEqual(BM25Index({}).search('退款', 5), [])
        self.assertEqual(BM25Index({'a': row('a', '')}).search('!!!', 5), [])

    def test_rrf_promotes_cross_route_consensus_and_retains_original_scores(self):
        a, b = row('a', '甲'), row('b', '乙')
        results = fuse_rankings({'vector': [dict(a, vector_score=999), dict(b, vector_score=-9)],
                                 'bm25': [dict(b, bm25_score=1)]}, 2)
        self.assertEqual([r['chunk_id'] for r in results], ['b', 'a'])
        self.assertAlmostEqual(results[0]['score'], 1 / 62 + 1 / 61)
        self.assertEqual(results[0]['retrieval_sources'], ['vector', 'bm25'])
        self.assertEqual(results[0]['vector_score'], -9)
        self.assertEqual(results[0]['bm25_score'], 1)
        self.assertNotIn('retrieval_sources', b)

    def test_rrf_deduplicates_each_route_and_preserves_distinct_chunks(self):
        a, b = row('a', '同内容'), row('b', '同内容')
        results = fuse_rankings({'vector': [a, a, b]}, 5)
        self.assertEqual(len(results), 2)
        self.assertAlmostEqual(results[0]['score'], 1 / 61)

    def test_hybrid_recovers_lexical_hit_missed_by_vector_candidates(self):
        kb = make_kb()
        for i in range(21):
            kb._collection.rows[str(i)] = ('普通登录说明', {'title': '账号'})
        kb._collection.rows['target'] = ('E_PAY_987 重复扣款', {'title': '支付'})
        results = kb.search('E_PAY_987', 5)
        target = next(r for r in results if r['chunk_id'] == 'target')
        self.assertEqual(target['retrieval_sources'], ['bm25'])
        self.assertEqual(len(results), 5)

    def test_existing_documents_are_indexed_without_reimport(self):
        kb = make_kb()
        kb._collection.rows['legacy-id'] = ('退款政策', {'title': '政策'})
        result = kb.search('退款')[0]
        self.assertEqual(result['chunk_id'], 'legacy-id')
        self.assertEqual(result['retrieval_sources'], ['vector', 'bm25'])

    def test_import_replaces_stale_lexical_content_immediately(self):
        kb = make_kb()
        kb.add_documents([{'title': '规则', 'content': 'OLD_CODE'}])
        self.assertTrue(kb._keyword_search('OLD_CODE', 5))
        kb.add_documents([{'title': '规则', 'content': 'NEW_CODE'}])
        self.assertEqual(kb._keyword_search('OLD_CODE', 5), [])
        self.assertTrue(kb._keyword_search('NEW_CODE', 5))

    def test_external_updates_refresh_on_count_change_and_after_ttl(self):
        kb = make_kb()
        kb._collection.rows['a'] = ('OLD_CODE', {})
        kb._keyword_search('OLD_CODE', 5)
        kb._collection.rows['a'] = ('NEW_CODE', {})
        kb._lexical_built_at -= 61
        self.assertTrue(kb._keyword_search('NEW_CODE', 5))
        self.assertEqual(kb._keyword_search('OLD_CODE', 5), [])
        kb._collection.rows['b'] = ('ADDED_CODE', {})
        self.assertTrue(kb._keyword_search('ADDED_CODE', 5))

    def test_index_is_cached_and_chroma_scan_is_paginated(self):
        kb = make_kb()
        for i in range(501):
            kb._collection.rows[str(i)] = ('hello', {})
        with patch.object(kb._collection, 'get', wraps=kb._collection.get) as get:
            kb._keyword_search('hello', 5)
            self.assertEqual(get.call_count, 2)
            self.assertEqual(get.call_args_list[1].kwargs['offset'], 500)
            kb._keyword_search('hello', 5)
            self.assertEqual(get.call_count, 2)

    def test_routes_execute_concurrently(self):
        kb = make_kb()
        barrier = threading.Barrier(2)

        def recall(*args):
            barrier.wait(timeout=2)
            return [row('a', '退款')]

        with patch.object(kb, '_vector_search', side_effect=recall), \
             patch.object(kb, '_keyword_search', side_effect=recall):
            self.assertEqual(kb.search('退款')[0]['retrieval_sources'], ['vector', 'bm25'])

    def test_one_failed_route_preserves_other_results(self):
        kb = make_kb()
        kb._collection.rows['a'] = ('退款', {})
        for method, remaining in [('_vector_search', 'bm25'), ('_keyword_search', 'vector')]:
            with patch.object(kb, method, side_effect=RuntimeError('offline')):
                result = kb.search('退款')[0]
            self.assertEqual(result['retrieval_sources'], [remaining])
            self.assertTrue(result['degraded_sources'])

    def test_route_failure_with_no_results_is_not_success(self):
        kb = make_kb()
        with patch.object(kb, '_vector_search', side_effect=RuntimeError('offline')):
            with self.assertRaises(RuntimeError):
                kb.search('退款')

    def test_empty_store_and_invalid_parameters(self):
        kb = make_kb()
        self.assertEqual(kb.search('退款'), [])
        for query, top_k in [('', 5), (' ', 5), (None, 5), ('x' * 2001, 5),
                             ('退款', 0), ('退款', True), ('退款', 21)]:
            with self.assertRaises(ValueError):
                kb.search(query, top_k)

    def test_async_tool_handler_uses_hybrid_retrieval(self):
        kb = make_kb()
        kb._collection.rows['a'] = ('退款', {})
        results = asyncio.run(kb.search_handler({'query': '退款', 'top_k': 1}, None))
        self.assertEqual(results[0]['retrieval_sources'], ['vector', 'bm25'])


if __name__ == '__main__':
    unittest.main()
