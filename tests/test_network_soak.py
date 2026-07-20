import os
import time

import pytest

pytest.importorskip('flask')

from blockchain.blockchain import Blockchain


pytestmark = pytest.mark.soak


@pytest.mark.skipif(
    os.environ.get('DENARIUS_RUN_SOAK') != '1',
    reason='set DENARIUS_RUN_SOAK=1 to run the network synchronization soak test',
)
def test_background_network_worker_survives_repeated_wakeups(tmp_path):
    blockchain = Blockchain()
    blockchain.STATE_PATH = tmp_path / 'soak.db'
    blockchain.start_background_sync(interval=1)
    try:
        for _ in range(1000):
            blockchain.trigger_background_sync()
        time.sleep(2)
        assert blockchain.synchronization_status()['running'] is True
        assert blockchain.synchronization_status()['last_error'] is None
    finally:
        blockchain.stop_background_sync()
        blockchain.network.close()
