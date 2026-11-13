import csv
import json
import tempfile
import unittest
from pathlib import Path

import build_dataset as target


def make_export(directory: Path):
    participants = [
        {
            "participantId": 1,
            "teamId": 100,
            "teamPosition": "TOP",
            "individualPosition": "TOP",
            "championId": 79,
            "championName": "Gragas",
            "puuid": "a",
            "riotIdGameName": "Alice",
            "riotIdTagline": "0001",
        },
        {
            "participantId": 2,
            "teamId": 200,
            "teamPosition": "TOP",
            "individualPosition": "TOP",
            "championId": 2,
            "championName": "Olaf",
            "puuid": "b",
            "riotIdGameName": "Bob",
            "riotIdTagline": "0002",
        },
    ]

    summary = {
        "games": [
            {
                "json": {
                    "gameId": 1,
                    "gameVersion": "16.22.836.1077",
                    "gameDuration": 600,
                    "queueId": 420,
                    "gameMode": "CLASSIC",
                    "mapId": 11,
                    "endOfGameResult": "GameComplete",
                    "teams": [
                        {"teamId": 100, "win": True},
                        {"teamId": 200, "win": False},
                    ],
                    "participants": participants,
                }
            }
        ]
    }

    frames = []

    for minute in range(11):
        frames.append(
            {
                "timestamp": minute * 60000,
                "participants": {
                    "1": {
                        "participantId": 1,
                        "level": 1 + minute // 3,
                        "currentGold": 500 + minute * 10,
                        "totalGold": 500 + minute * 100,
                        "xp": minute * 120,
                        "cs": minute * 6,
                    },
                    "2": {
                        "participantId": 2,
                        "level": 1 + minute // 3,
                        "currentGold": 500 + minute * 8,
                        "totalGold": 500 + minute * 90,
                        "xp": minute * 100,
                        "cs": minute * 5,
                    },
                },
            }
        )

    timeline = {
        "gameId": 1,
        "players": [
            {"participantId": 1, "teamId": 100, "position": "TOP"},
            {"participantId": 2, "teamId": 200, "position": "TOP"},
        ],
        "frames": frames,
        "events": [
            {
                "timestamp": 90000,
                "type": "CHAMPION_KILL",
                "actorId": 1,
                "victimId": 2,
                "assistIds": [],
            },
            {
                "timestamp": 300000,
                "type": "BUILDING_KILL",
                "actorId": 1,
                "buildingType": "TOWER_BUILDING",
                "towerType": "OUTER_TURRET",
                "laneType": "TOP_LANE",
            },
        ],
    }

    (directory / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False), encoding="utf-8"
    )
    (directory / "timeline").mkdir()
    (directory / "timeline" / "timeline.json").write_text(
        json.dumps(timeline, ensure_ascii=False), encoding="utf-8"
    )


class DatasetTests(unittest.TestCase):
    def test_builds_blue_and_side_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            export = directory / "export"
            export.mkdir()
            make_export(export)
            output = directory / "dataset"

            code = target.build([export], output, (420,), 600, True)
            self.assertEqual(code, 0)

            rows = list(
                csv.DictReader(
                    (output / "dataset_blue.csv").open(encoding="utf-8-sig")
                )
            )
            sides = list(
                csv.DictReader(
                    (output / "dataset_sides.csv").open(encoding="utf-8-sig")
                )
            )

            self.assertEqual(len(rows), 10)
            self.assertEqual(len(sides), 20)

            minute_two = next(row for row in rows if row["minute"] == "2")
            self.assertEqual(minute_two["kills_last_min"], "1")
            self.assertEqual(minute_two["kills_diff"], "1")

            minute_five = next(row for row in rows if row["minute"] == "5")
            self.assertEqual(minute_five["tower_diff"], "1")

            red = next(
                row
                for row in sides
                if row["minute"] == "5" and row["side"] == "red"
            )
            blue = next(
                row
                for row in sides
                if row["minute"] == "5" and row["side"] == "blue"
            )
            self.assertEqual(float(red["gold_diff"]), -float(blue["gold_diff"]))
            self.assertEqual(red["win"], str(1 - int(blue["blue_win"])))

            split = json.loads((output / "split.json").read_text(encoding="utf-8"))
            self.assertIn(1, split["train"] + split["val"] + split["test"])


if __name__ == "__main__":
    unittest.main()
