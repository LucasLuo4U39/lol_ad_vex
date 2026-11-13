import tempfile
import unittest
from pathlib import Path

import match_export as target


class ParserTests(unittest.TestCase):
    def test_parse_command_line(self):
        command_line = (
            "LeagueClientUx.exe --app-port=58662 "
            "--remoting-auth-token=abc_DEF-123 --app-pid=35668 "
            "--rso_platform_id=TJ100 --region=TENCENT"
        )

        auth = target.parse_command_line(command_line)

        self.assertIsNotNone(auth)
        assert auth is not None
        self.assertEqual(auth.port, 58662)
        self.assertEqual(auth.token, "abc_DEF-123")
        self.assertEqual(auth.pid, 35668)
        self.assertEqual(auth.region, "TENCENT")
        self.assertEqual(auth.rso_platform_id, "TJ100")

    def test_parse_command_line_requires_port_and_token(self):
        self.assertIsNone(target.parse_command_line("LeagueClientUx.exe --region=NA"))


class SgpMappingTests(unittest.TestCase):
    def test_tencent_server_id(self):
        self.assertEqual(
            target.get_sgp_server_id("TENCENT", "TJ100"), "TENCENT_TJ100"
        )

    def test_region_alias(self):
        self.assertEqual(target.get_sgp_server_id("NA", ""), "NA1")
        self.assertEqual(target.get_sgp_server_id("EUW1", ""), "EUW")

    def test_sub_id(self):
        tencent = target.SGP_SERVERS["TENCENT_TJ100"]
        euw = target.SGP_SERVERS["EUW"]

        self.assertEqual(target.sgp_sub_id("TENCENT_TJ100", tencent), "TJ100")
        self.assertEqual(target.sgp_sub_id("EUW", euw), "EUW1")


class CsvTests(unittest.TestCase):
    def test_builds_one_row_for_target_player(self):
        game = {
            "gameId": 1001,
            "gameCreation": 1767268800000,
            "gameDuration": 1800,
            "queueId": 420,
            "gameMode": "CLASSIC",
            "participants": [
                {
                    "puuid": "me",
                    "championId": 103,
                    "championName": "Ahri",
                    "teamId": 100,
                    "individualPosition": "MIDDLE",
                    "teamPosition": "MIDDLE",
                    "kills": 10,
                    "deaths": 2,
                    "assists": 5,
                    "totalMinionsKilled": 200,
                    "goldEarned": 12000,
                    "visionScore": 20,
                    "wardsPlaced": 5,
                    "totalDamageDealtToChampions": 25000,
                    "totalDamageTaken": 18000,
                    "doubleKills": 1,
                    "tripleKills": 0,
                    "quadraKills": 0,
                    "pentaKills": 0,
                    "win": True,
                }
            ],
        }

        rows = target.build_csv_rows([game], "me")

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], "gameId")
        self.assertIn("7.50", rows[1])
        self.assertIn("Ahri", rows[1])

    def test_skips_games_without_target_player(self):
        game = {
            "gameId": 1002,
            "gameCreation": 1767268800000,
            "gameDuration": 1800,
            "queueId": 420,
            "gameMode": "CLASSIC",
            "participants": [{"puuid": "someone-else"}],
        }

        self.assertEqual(len(target.build_csv_rows([game], "me")), 1)


class ReplayLookupTests(unittest.TestCase):
    def test_finds_prefixed_and_nested_replays(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nested = root / "NA1"
            nested.mkdir()
            (nested / "NA1_67890.rofl").write_bytes(b"x")
            (root / "12345.rofl").write_bytes(b"x")
            (root / "123456.rofl").write_bytes(b"x")

            self.assertEqual(
                target.find_replay_file(root, 12345), root / "12345.rofl"
            )
            self.assertEqual(
                target.find_replay_file(root, 67890), nested / "NA1_67890.rofl"
            )
            self.assertIsNone(target.find_replay_file(root, 1234))


class PathTests(unittest.TestCase):
    def test_sanitize(self):
        self.assertEqual(
            target.sanitize_path_segment('a<b>c:d"e/f\\g|h?i*j', "fallback"),
            "a_b_c_d_e_f_g_h_i_j",
        )
        self.assertEqual(target.sanitize_path_segment("   ", "fallback"), "fallback")


if __name__ == "__main__":
    unittest.main()
