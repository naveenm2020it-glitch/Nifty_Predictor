import io
import unittest
import zipfile

from backend.pipeline import _clean_number, _csv_rows, _parse_date, _participant_rows


class PipelineParserTests(unittest.TestCase):
    def test_parse_exchange_date_formats(self):
        self.assertEqual(str(_parse_date("10-Nov-2026")), "2026-11-10")
        self.assertEqual(str(_parse_date("20261110")), "2026-11-10")
        self.assertIsNone(_parse_date("not-a-date"))

    def test_clean_number_handles_commas_and_empty_values(self):
        self.assertEqual(_clean_number("1,234.50"), 1234.5)
        self.assertIsNone(_clean_number(""))
        self.assertIsNone(_clean_number("N/A"))

    def test_participant_report_skips_title_preamble(self):
        raw = (
            "NSE Clearing Ltd,Participant-wise Open Interest\n"
            "Report Date,10-Nov-2026\n"
            "Client Type,Future Index Long,Future Index Short\n"
            "FII,100,80\n"
            "DII,50,60\n"
        ).encode()
        rows = _participant_rows(raw)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["Client Type"], "FII")
        self.assertEqual(rows[1]["Future Index Short"], "60")

    def test_zip_bhavcopy_csv_is_read(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("bhavcopy.csv", "TckrSymb,ClsPric\nNIFTY,25000\n")
        rows = _csv_rows(buffer.getvalue(), zipped=True)
        self.assertEqual(rows[0]["TckrSymb"], "NIFTY")
        self.assertEqual(rows[0]["ClsPric"], "25000")


if __name__ == "__main__":
    unittest.main()
