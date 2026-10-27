# -*- coding: utf-8 -*-
"""Тесты простых 8-буквенных кириллических токенов."""
import hashlib
import unittest
import urllib.parse

import token_store


class SimpleTokenTests(unittest.TestCase):
    def test_normalize_is_case_insensitive_and_trims(self):
        self.assertEqual(
            token_store.normalize_simple_token('  ПРИМЕРТК '),
            token_store.normalize_simple_token('примертк'))

    def test_normalize_uses_nfc(self):
        decomposed = 'й' + 'абвгдеж'   # й разложенная + 7 букв
        self.assertEqual(token_store.normalize_simple_token(decomposed),
                         token_store.normalize_simple_token('йабвгдеж'))

    def test_is_simple_token_accepts_exactly_eight_cyrillic_letters(self):
        self.assertTrue(token_store.is_simple_token('абвгдеёж'))
        self.assertTrue(token_store.is_simple_token('ПРИМЕРТК'))
        self.assertFalse(token_store.is_simple_token('абвгдеж'))
        self.assertFalse(token_store.is_simple_token('абвгдежзи'))
        self.assertFalse(token_store.is_simple_token('abcdefgh'))
        self.assertFalse(token_store.is_simple_token('абвгдеж1'))
        self.assertFalse(token_store.is_simple_token('абвг деж'))
        self.assertFalse(token_store.is_simple_token(None))

    def test_hash_is_case_insensitive_and_namespaced(self):
        lower = token_store.simple_token_hash('примертк')
        upper = token_store.simple_token_hash('ПРИМЕРТК')
        self.assertEqual(lower, upper)
        self.assertNotEqual(lower, hashlib.sha256('примертк'.encode('utf-8')).hexdigest())
        self.assertEqual(len(lower), 64)

    def test_hash_rejects_wrong_length(self):
        with self.assertRaises(ValueError):
            token_store.simple_token_hash('коротко')
        with self.assertRaises(ValueError):
            token_store.simple_token_hash('абвгдеж1')

    def test_generate_returns_eight_cyrillic_letters(self):
        for _ in range(20):
            token = token_store.generate_simple_token()
            self.assertEqual(len(token), 8)
            self.assertTrue(token_store.is_simple_token(token))

    def test_decode_percent_encoded_header(self):
        encoded = urllib.parse.quote('ПРИМЕРТК')
        self.assertEqual(token_store.decode_simple_token(encoded), 'примертк')

    def test_decode_recovers_utf8_mojibake(self):
        mojibake = 'ПРИМЕРТК'.encode('utf-8').decode('latin-1')
        self.assertEqual(token_store.decode_simple_token(mojibake), 'примертк')

    def test_decode_leaves_legacy_keys_untouched(self):
        legacy = 'alb_' + 'a1' * 24
        self.assertEqual(token_store.decode_simple_token(legacy), legacy)

    def test_hash_candidates_include_simple_and_decoded(self):
        encoded = urllib.parse.quote('Пролонга')
        candidates = token_store.api_key_hash_candidates(encoded)
        self.assertIn(token_store.simple_token_hash('примертк'), candidates)
        self.assertIn(hashlib.sha256('примертк'.encode('utf-8')).hexdigest(), candidates)
        # Исходное percent-encoded значение тоже проверяется (legacy-совместимость).
        self.assertIn(hashlib.sha256(encoded.encode('utf-8')).hexdigest(), candidates)

    def test_hash_candidates_for_legacy_key(self):
        legacy = 'alb_' + 'b2' * 24
        candidates = token_store.api_key_hash_candidates(legacy)
        self.assertEqual(candidates[0], hashlib.sha256(legacy.encode('utf-8')).hexdigest())


if __name__ == '__main__':
    unittest.main()
