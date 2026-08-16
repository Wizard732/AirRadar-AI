import unittest

from media_ocr import extract_text_with_ocr


class _Message:
    text = "Caption"
    message = "Caption"
    photo = object()


class _Event:
    message = _Message()


class OcrSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_blank_vision_model_returns_caption_without_download(self):
        result = await extract_text_with_ocr(_Event(), None, "key", "   ")
        self.assertEqual(result, "Caption")


if __name__ == "__main__":
    unittest.main()
