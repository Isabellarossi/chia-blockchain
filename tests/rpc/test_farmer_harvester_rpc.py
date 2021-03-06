import asyncio

import pytest

from secrets import token_bytes
from blspy import PrivateKey
from chiapos import DiskPlotter
from src.rpc.farmer_rpc_client import FarmerRpcClient
from src.rpc.harvester_rpc_client import HarvesterRpcClient
from src.rpc.rpc_server import start_rpc_server
from src.util.ints import uint16
from src.util.config import load_config
from src.plotting.plot_tools import stream_plot_info
from src.rpc.farmer_rpc_api import FarmerRpcApi
from src.rpc.harvester_rpc_api import HarvesterRpcApi

from tests.setup_nodes import setup_farmer_harvester, test_constants, bt
from src.util.block_tools import get_plot_dir
from tests.time_out_assert import time_out_assert


@pytest.fixture(scope="module")
def event_loop():
    loop = asyncio.get_event_loop()
    yield loop


class TestRpc:
    @pytest.fixture(scope="function")
    async def simulation(self):
        async for _ in setup_farmer_harvester(test_constants.copy()):
            yield _

    @pytest.mark.asyncio
    async def test1(self, simulation):
        test_rpc_port = uint16(21522)
        test_rpc_port_2 = uint16(21523)
        harvester, farmer = simulation

        def stop_node_cb():
            pass

        def stop_node_cb_2():
            pass

        config = load_config(bt.root_path, "config.yaml")
        hostname = config["self_hostname"]
        daemon_port = config["daemon_port"]

        farmer_rpc_api = FarmerRpcApi(farmer)
        harvester_rpc_api = HarvesterRpcApi(harvester)

        rpc_cleanup = await start_rpc_server(
            farmer_rpc_api,
            hostname,
            daemon_port,
            test_rpc_port,
            stop_node_cb,
            connect_to_daemon=False,
        )
        rpc_cleanup_2 = await start_rpc_server(
            harvester_rpc_api,
            hostname,
            daemon_port,
            test_rpc_port_2,
            stop_node_cb_2,
            connect_to_daemon=False,
        )

        try:
            client = await FarmerRpcClient.create("localhost", test_rpc_port)
            client_2 = await HarvesterRpcClient.create("localhost", test_rpc_port_2)

            async def have_connections():
                return len(await client.get_connections()) > 0

            await time_out_assert(5, have_connections, True)

            await client.get_latest_challenges()

            async def have_challenges():
                return len(await client.get_latest_challenges()) > 0

            await time_out_assert(5, have_challenges, True)

            async def have_plots():
                return len((await client_2.get_plots())["plots"]) > 0

            await time_out_assert(5, have_plots, True)

            res = await client_2.get_plots()
            num_plots = len(res["plots"])
            assert num_plots > 0
            plot_dir = get_plot_dir() / "subdir"
            plot_dir.mkdir(parents=True, exist_ok=True)
            plotter = DiskPlotter()
            filename = "test_farmer_harvester_rpc_plot.plot"
            plotter.create_plot_disk(
                str(plot_dir),
                str(plot_dir),
                str(plot_dir),
                filename,
                18,
                stream_plot_info(
                    bt.pool_pk, bt.farmer_pk, PrivateKey.from_seed(bytes([4] * 32))
                ),
                token_bytes(32),
                128,
            )

            res_2 = await client_2.get_plots()
            assert len(res_2["plots"]) == num_plots

            await client_2.add_plot_directory(str(plot_dir))

            res_2 = await client_2.get_plots()
            assert len(res_2["plots"]) == num_plots + 1

            await client_2.delete_plot(str(plot_dir / filename))
            res_3 = await client_2.get_plots()
            assert len(res_3["plots"]) == num_plots

        except AssertionError:
            # Checks that the RPC manages to stop the node
            client.close()
            client_2.close()
            await client.await_closed()
            await client_2.await_closed()
            await rpc_cleanup()
            await rpc_cleanup_2()
            raise

        client.close()
        client_2.close()
        await client.await_closed()
        await client_2.await_closed()
        await rpc_cleanup()
        await rpc_cleanup_2()
