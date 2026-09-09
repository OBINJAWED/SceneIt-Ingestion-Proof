"""Seed the controlled restart fixture. Refuses non-test/provider-enabled use."""
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "artifacts" / "api-server"))

from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb

from sceneit.db import PROOF_ID, connection


url = os.environ.get("DATABASE_URL", "")
database = conninfo_to_dict(url).get("dbname", "") if url else ""
if not database.startswith("sceneit_test"):
    raise SystemExit("Restart fixture requires a sceneit_test* database")
if os.environ.get("SCENEIT_DISABLE_PROVIDER_NETWORK") != "1":
    raise SystemExit("Restart fixture requires provider networking disabled")
if os.environ.get("TWELVE_LABS_API_KEY"):
    raise SystemExit("Restart fixture refuses provider credentials")

search_id = uuid.uuid4()
with connection() as conn:
    conn.execute(
        "INSERT INTO sceneit_proofs(id,title,youtube_id,source_path,source_sha256,media,"
        "state,index_name,index_id,asset_id,indexed_asset_id,searches_used) "
        "VALUES(%s,'Fixture proof','abcdefghijk','not-in-workspace','fixture-sha',%s,"
        "'ready','fixture-index','index-id','asset-id','indexed-id',37)",
        (PROOF_ID, Jsonb({
            "duration": 100, "width": 640, "height": 360,
            "hasAudio": True, "size": 1000,
        })),
    )
    conn.execute(
        "INSERT INTO sceneit_searches(id,proof_id,query,query_key,modality,state,"
        "matches,attempt_id,deadline_at,completed_at) "
        "VALUES(%s,%s,'Fixture saved result','fixture saved result','visual','done',"
        "%s,%s,now(),now())",
        (
            search_id,
            PROOF_ID,
            Jsonb([{"rank": 1, "startSeconds": 10, "endSeconds": 15}]),
            uuid.uuid4(),
        ),
    )
print("Seeded controlled saved-proof restart fixture with quota usage 37.")