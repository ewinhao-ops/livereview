from __future__ import annotations

import unittest
from io import BytesIO

from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
)

from app.core import document_text, embed


def _pdf_with_content(content: bytes) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    font_ref = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref}),
    })
    stream = DecodedStreamObject()
    stream.set_data(content)
    page[NameObject("/Contents")] = writer._add_object(stream.flate_encode())
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


class TokenSamplingTests(unittest.TestCase):
    def test_short_text_keeps_all_tokens(self) -> None:
        self.assertEqual(
            set(embed._tokens("Hello 直播技巧")),
            {"hello", "直播", "直播技", "播技", "播技巧", "技巧"},
        )

    def test_long_text_sampling_is_deterministic_and_covers_full_text(self) -> None:
        text = "".join(chr(0x4E00 + index) for index in range(500))
        first = embed._tokens(text)
        self.assertEqual(first, embed._tokens(text))
        self.assertEqual(len(first), embed._MAX_KEYWORD_TOKENS)
        positions = [text.find(token) for token in first]
        self.assertLessEqual(min(positions), 1)
        self.assertGreaterEqual(max(positions), len(text) - 3)
        for tenth in range(10):
            lower = tenth * len(text) // 10
            upper = (tenth + 1) * len(text) // 10
            self.assertTrue(any(lower <= position < upper for position in positions))


class PdfResourceGuardTests(unittest.TestCase):
    def test_normal_small_pdf_remains_extractable(self) -> None:
        data = _pdf_with_content(b"BT /F1 12 Tf 10 100 Td (normal pdf text) Tj ET")
        self.assertIn("normal pdf text", document_text.extract_document_text("normal.pdf", data))

    def test_compressed_single_page_content_over_limit_is_rejected(self) -> None:
        oversized = b" " * (document_text._PDF_MAX_PAGE_DECODED_CONTENT + 1)
        data = _pdf_with_content(oversized)
        self.assertLess(len(data), document_text._PDF_MAX_PAGE_COMPRESSED_CONTENT)
        with self.assertRaisesRegex(ValueError, "单页解压后内容流过大"):
            document_text.extract_document_text("oversized.pdf", data)

    def test_excessive_page_content_stream_count_is_rejected(self) -> None:
        writer = PdfWriter()
        page = writer.add_blank_page(width=100, height=100)
        refs = []
        for _ in range(document_text._PDF_MAX_CONTENT_STREAMS_PER_PAGE + 1):
            stream = DecodedStreamObject()
            stream.set_data(b" ")
            refs.append(writer._add_object(stream))
        page[NameObject("/Contents")] = ArrayObject(refs)
        output = BytesIO()
        writer.write(output)
        with self.assertRaisesRegex(ValueError, "单页内容流过多"):
            document_text.extract_document_text("many-streams.pdf", output.getvalue())


if __name__ == "__main__":
    unittest.main()
