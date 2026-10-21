import unicodedata
import unittest

from utils.fusion_design import feature_kind, read_features, Design


class FeatureKindTest(unittest.TestCase):
    def test_portuguese_tags_become_english(self):
        self.assertEqual(feature_kind("Esboço"), "Sketch")
        self.assertEqual(feature_kind("Extrusão"), "Extrude")
        self.assertEqual(feature_kind("Plano de trabalho"), "WorkPlane")

    def test_decomposed_accents_still_match(self):
        self.assertEqual(feature_kind(unicodedata.normalize("NFD", "Extrusão")), "Extrude")

    def test_english_and_unknown_tags_pass_through(self):
        self.assertEqual(feature_kind("Extrude"), "Extrude")
        self.assertEqual(feature_kind("Padrão retangular"), "Padrão retangular")
        self.assertEqual(feature_kind(""), "")

    def test_read_features_keeps_the_localized_name(self):
        design = Design.__new__(Design)
        design.name, design.notes, design.features, design.feature_ids = "d", [], [], {}
        read_features(design, {"features": [{"id": "f1", "ordinal": 0, "name": "Extrusão 1", "source_tag": "Extrusão"}]}, [])
        self.assertEqual(design.features[0].kind, "Extrude")
        self.assertEqual(design.features[0].name, "Extrusão 1")


if __name__ == "__main__":
    unittest.main()
