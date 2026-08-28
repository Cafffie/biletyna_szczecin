"""Test script for the Szczecin Theatre extractor."""
from scrapers.biletyna_szczecin.run_extractor import BiletynaSzczecinExtractor

from utils.logger import setup_logger

logger = setup_logger(__name__, log_to_file=False)


def test_biletyna_szczecin_extractor():
    """Test the Szczecin Theatre extractor against a small sample of shows."""
    logger.info("Starting Szczecin Theatre extractor test...")

    extractor = BiletynaSzczecinExtractor(
        local_test=True,
        show_count=5,
        save_csv_locally=True,
        csv_incremental_mode=False,
        log_to_file=True,
        log_to_terminal=True,
    )

    result = extractor.run()

    logger.info(f"Test completed with result: {result}")
    return result


if __name__ == "__main__":
    test_biletyna_szczecin_extractor()
