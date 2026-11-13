import json
import tempfile
import unittest
from pathlib import Path

import build_timeline as target


def participant_frame(level, current_gold, total_gold, xp, cs):
    return {
        "level": level,
        "currentGold": current_gold,
        "totalGold": total_gold,
        "xp": xp,
        "minionsKilled": cs,
        "jungleMinionsKilled": 0,
        "position": {"x": 100, "y": 200},
    }


def make_fixture(directory: Path):
    summary = {
        "puuid": "a",
        "games": [
            {
                "json": {
                    "gameId": 123,
                    "gameVersion": "16.22.836.1077",
                    "gameDuration": 120,
                    "queueId": 420,
                    "gameMode": "CLASSIC",
                    "participants": [
                        {
                            "participantId": 1,
                            "puuid": "a",
                            "riotIdGameName": "Alice",
                            "riotIdTagline": "0001",
                            "championName": "Ahri",
                            "teamId": 100,
                            "teamPosition": "MIDDLE",
                            "individualPosition": "MIDDLE",
                        },
                        {
                            "participantId": 2,
                            "puuid": "b",
                            "riotIdGameName": "Bob",
                            "riotIdTagline": "0002",
                            "championName": "Garen",
                            "teamId": 200,
                            "teamPosition": "TOP",
                            "individualPosition": "TOP",
                        },
                    ],
                }
            }
        ],
    }

    details_record = {
        "gameId": 123,
        "details": {
            "metadata": {},
            "json": {
                "frameInterval": 60000,
                "gameId": 123,
                "participants": [
                    {"participantId": 1, "puuid": "a"},
                    {"participantId": 2, "puuid": "b"},
                ],
                "frames": [
                    {
                        "timestamp": 0,
                        "events": [
                            {
                                "type": "ITEM_PURCHASED",
                                "participantId": 1,
                                "itemId": 1056,
                                "timestamp": 0,
                            }
                        ],
                        "participantFrames": {
                            "1": participant_frame(1, 500, 500, 0, 0),
                            "2": participant_frame(1, 500, 500, 0, 0),
                        },
                    },
                    {
                        "timestamp": 60000,
                        "events": [
                            {
                                "type": "CHAMPION_KILL",
                                "killerId": 1,
                                "victimId": 2,
                                "assistingParticipantIds": [],
                                "timestamp": 60000,
                                "position": {"x": 10, "y": 20},
                            }
                        ],
                        "participantFrames": {
                            "1": participant_frame(7, 500, 3500, 4200, 80),
                            "2": participant_frame(6, 300, 2800, 3600, 65),
                        },
                    },
                ],
            },
        },
    }

    (directory / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False), encoding="utf-8"
    )
    (directory / "details.jsonl").write_text(
        json.dumps(details_record, ensure_ascii=False) + "\n", encoding="utf-8"
    )


class TimelineTests(unittest.TestCase):
    def test_builds_events_with_gold_and_xp(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            make_fixture(directory)
            output = directory / "timeline"

            code = target.build(
                directory / "summary.json",
                directory / "details.jsonl",
                output,
                None,
                False,
            )

            self.assertEqual(code, 0)
            timeline = json.loads(
                (output / "timeline.json").read_text(encoding="utf-8")
            )

            self.assertEqual(timeline["gameId"], 123)
            self.assertEqual(len(timeline["players"]), 2)
            self.assertEqual(len(timeline["frames"]), 2)

            kill = next(
                event for event in timeline["events"] if event["type"] == "CHAMPION_KILL"
            )
            self.assertEqual(kill["actorName"], "Alice#0001")
            self.assertEqual(kill["actorState"]["currentGold"], 500)
            self.assertEqual(kill["actorState"]["totalGold"], 3500)
            self.assertEqual(kill["actorState"]["xp"], 4200)
            self.assertEqual(kill["victimState"]["level"], 6)

            self.assertTrue((output / "timeline_events.csv").is_file())
            self.assertTrue((output / "timeline_frames.csv").is_file())
            self.assertTrue((output / "timeline.md").is_file())

    def test_mmss(self):
        self.assertEqual(target.mmss(0), "00:00")
        self.assertEqual(target.mmss(107668), "01:47")
        self.assertEqual(target.mmss(3661000), "1:01:01")


if __name__ == "__main__":
    unittest.main()
