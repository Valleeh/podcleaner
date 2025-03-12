import os
import json
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from pydub import AudioSegment

# Import the classes from your module.
# (Adjust the import path if needed.)
from podcast_processor import (
    PodcastDownloader,
    Transcriber,
    TranscriptNormalizer,
    AdDetector,
    AudioProcessor,
    PodcastProcessor,
)

# ------------------------------------------------------------------------------
# Tests for PodcastDownloader
# ------------------------------------------------------------------------------
class TestPodcastDownloader(unittest.TestCase):
    @patch("podcast_processor.requests.get")
    def test_download_creates_file(self, mock_get):
        # Prepare a fake response with dummy content.
        fake_content = b"fake audio content"
        fake_response = MagicMock()
        fake_response.iter_content.return_value = [fake_content]
        fake_response.raise_for_status = lambda: None
        mock_get.return_value = fake_response

        with tempfile.TemporaryDirectory() as tmpdir:
            downloader = PodcastDownloader(download_dir=tmpdir)
            test_url = "https://example.com/test.mp3"
            file_path = downloader.download(test_url)
            # Check that the file exists.
            self.assertTrue(os.path.exists(file_path))
            with open(file_path, "rb") as f:
                self.assertEqual(f.read(), fake_content)
            # Calling download again should return the same file (no redownload)
            file_path2 = downloader.download(test_url)
            self.assertEqual(file_path, file_path2)

# ------------------------------------------------------------------------------
# Tests for Transcriber
# ------------------------------------------------------------------------------
class TestTranscriber(unittest.TestCase):
    @patch("podcast_processor.whisper.load_model")
    def test_transcribe_creates_transcript(self, mock_load_model):
        # Create a fake Whisper model that returns dummy transcription.
        fake_model = MagicMock()
        fake_result = {"transcription": [{"text": "Hello world", "start": 0, "end": 1}]}
        fake_model.transcribe.return_value = fake_result
        mock_load_model.return_value = fake_model

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a dummy audio file.
            audio_file = os.path.join(tmpdir, "audio.mp3")
            with open(audio_file, "wb") as f:
                f.write(b"dummy audio")
            transcriber = Transcriber()
            transcript_file = transcriber.transcribe(audio_file)
            self.assertTrue(os.path.exists(transcript_file))
            with open(transcript_file, "r") as f:
                data = json.load(f)
                self.assertEqual(data, fake_result)
            # Calling transcribe again should not change the result.
            transcript_file2 = transcriber.transcribe(audio_file)
            self.assertEqual(transcript_file, transcript_file2)

# ------------------------------------------------------------------------------
# Tests for TranscriptNormalizer
# ------------------------------------------------------------------------------
class TestTranscriptNormalizer(unittest.TestCase):
    def test_normalize_adds_segment_ids(self):
        # Create dummy transcript JSON data.
        dummy_data = {
            "transcription": [
                {"text": "Segment 1", "start": 0, "end": 1},
                {"text": "Segment 2", "start": 1, "end": 2},
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            input_file = os.path.join(tmpdir, "transcript.json")
            with open(input_file, "w") as f:
                json.dump(dummy_data, f)
            normalizer = TranscriptNormalizer()
            output_file = normalizer.normalize(input_file)
            self.assertTrue(os.path.exists(output_file))
            with open(output_file, "r") as f:
                data = json.load(f)
            # Check that every segment now has a "segment_id" field.
            for i, segment in enumerate(data["transcription"]):
                self.assertEqual(segment.get("segment_id"), i)

# ------------------------------------------------------------------------------
# Tests for AdDetector
# ------------------------------------------------------------------------------
class TestAdDetector(unittest.TestCase):
    @patch("podcast_processor.openai.ChatCompletion.create")
    def test_process_chunk(self, mock_chat_create):
        # Set up a fake response from OpenAI.
        fake_response_content = '{"segments": [{"id": 0, "ad": true}, {"id": 1, "ad": false}]}'
        fake_choice = MagicMock()
        fake_choice.message = MagicMock(content=fake_response_content)
        fake_response = MagicMock(choices=[fake_choice])
        mock_chat_create.return_value = fake_response

        # Create a fake chunk of transcript data.
        chunk_data = [(0, "Ad content"), (1, "Regular content")]
        detector = AdDetector(chunk_size=2)
        results = detector.process_chunk(chunk_data, chunk_id=0)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["id"], 0)
        self.assertTrue(results[0]["ad"])
        self.assertEqual(results[1]["id"], 1)
        self.assertFalse(results[1]["ad"])

    @patch("podcast_processor.openai.ChatCompletion.create")
    def test_process_transcript(self, mock_chat_create):
        # Prepare fake response content for each chunk.
        fake_response_content = '{"segments": [{"id": 0, "ad": true}, {"id": 1, "ad": false}]}'
        fake_choice = MagicMock()
        fake_choice.message = MagicMock(content=fake_response_content)
        fake_response = MagicMock(choices=[fake_choice])
        mock_chat_create.return_value = fake_response

        # Create dummy normalized transcript data.
        dummy_data = {
            "transcription": [
                {"text": "Ad segment", "start": 0, "end": 1, "segment_id": 0},
                {"text": "Content segment", "start": 1, "end": 2, "segment_id": 1},
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            transcript_file = os.path.join(tmpdir, "norm.json")
            with open(transcript_file, "w") as f:
                json.dump(dummy_data, f)
            detector = AdDetector(chunk_size=2)
            results = detector.process_transcript(transcript_file)
            self.assertEqual(len(results), 2)
            self.assertEqual(results[0]["id"], 0)
            self.assertEqual(results[1]["id"], 1)

    def test_results_to_dict(self):
        detector = AdDetector()
        sample_results = [{"id": 0, "ad": True}, {"id": 1, "ad": False}]
        result_dict = detector.results_to_dict(sample_results)
        self.assertEqual(result_dict, {0: 1, 1: 0})

# ------------------------------------------------------------------------------
# Tests for AudioProcessor
# ------------------------------------------------------------------------------
class TestAudioProcessor(unittest.TestCase):
    def test_merge_segments(self):
        processor = AudioProcessor()
        # Example start and end times.
        starts = [0, 5, 15]
        ends = [4, 10, 20]
        merged_starts, merged_ends = processor.merge_segments(starts, ends, min_duration=2, max_gap=3)
        # With a gap of 1 between 4 and 5, expect the first two segments to merge.
        self.assertEqual(merged_starts, [0, 15])
        self.assertEqual(merged_ends, [10, 20])

    def test_remove_ads(self):
        # Create a 10-second silent audio for testing.
        audio = AudioSegment.silent(duration=10000)
        with tempfile.TemporaryDirectory() as tmpdir:
            input_file = os.path.join(tmpdir, "input.mp3")
            output_file = os.path.join(tmpdir, "output.mp3")
            audio.export(input_file, format="mp3")
            processor = AudioProcessor()
            # Remove the segment from 2s to 4s.
            processor.remove_ads(input_file, output_file, [2], [4])
            self.assertTrue(os.path.exists(output_file))
            out_audio = AudioSegment.from_mp3(output_file)
            # Expect the output duration to be roughly 8 seconds.
            self.assertAlmostEqual(len(out_audio) / 1000.0, 8, delta=0.5)

# ------------------------------------------------------------------------------
# Tests for PodcastProcessor (Pipeline)
# ------------------------------------------------------------------------------
class TestPodcastProcessor(unittest.TestCase):
    @patch("podcast_processor.AudioProcessor.merge_segments")
    @patch("podcast_processor.AudioProcessor.remove_ads")
    @patch("podcast_processor.AdDetector.process_transcript")
    @patch("podcast_processor.TranscriptNormalizer.normalize")
    @patch("podcast_processor.Transcriber.transcribe")
    @patch("podcast_processor.PodcastDownloader.download")
    def test_process_pipeline(self, mock_download, mock_transcribe, mock_normalize,
                              mock_process_transcript, mock_remove_ads, mock_merge):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Set up fake file paths.
            fake_audio_file = os.path.join(tmpdir, "audio.mp3")
            fake_processed_file = fake_audio_file + "_processed.mp3"
            # Create a dummy audio file.
            with open(fake_audio_file, "wb") as f:
                f.write(b"dummy audio")
            mock_download.return_value = fake_audio_file
            fake_transcript_file = os.path.join(tmpdir, "transcript.json")
            # Create dummy transcript data.
            dummy_transcript = {
                "transcription": [
                    {"text": "Ad", "start": 0, "end": 1, "segment_id": 0},
                    {"text": "Content", "start": 1, "end": 2, "segment_id": 1},
                ]
            }
            with open(fake_transcript_file, "w") as f:
                json.dump(dummy_transcript, f)
            mock_transcribe.return_value = fake_transcript_file
            mock_normalize.return_value = fake_transcript_file
            # Simulate ad detection: mark segment 0 as ad.
            mock_process_transcript.return_value = [{"id": 0, "ad": True}, {"id": 1, "ad": False}]
            # Simulate merge_segments to return fixed ad segments.
            mock_merge.return_value = ([0], [1])
            # Create processor instance with real (but now mocked) dependencies.
            downloader = PodcastDownloader()
            transcriber = Transcriber()
            normalizer = TranscriptNormalizer()
            ad_detector = AdDetector(chunk_size=2)
            audio_processor = AudioProcessor()
            processor = PodcastProcessor(downloader, transcriber, normalizer, ad_detector, audio_processor)
            # Run the processing pipeline.
            result = processor.process("https://example.com/fake.mp3")
            # Verify that remove_ads was called and the processed file path is returned.
            mock_remove_ads.assert_called()
            self.assertEqual(result, fake_audio_file + "_processed.mp3")

# ------------------------------------------------------------------------------
# Run the tests
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    unittest.main()
