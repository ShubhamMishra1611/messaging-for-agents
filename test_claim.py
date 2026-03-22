"""Reproduce & verify fix for rowcount==0 bug with pg_insert + ON CONFLICT DO NOTHING + psycopg3."""

import asyncio
import uuid

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import text

from db.models import Base, Claim

DB_URL = "postgresql+psycopg://postgres:postgres@localhost:5433/agent_messaging"

engine = create_async_engine(DB_URL, echo=True)
session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def setup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # clean slate
    async with session_factory() as s:
        await s.execute(text("DELETE FROM claims"))
        await s.commit()


async def test_bug_rowcount():
    """Original code — rowcount is 0 even on successful insert."""
    msg_id = uuid.uuid4()
    async with session_factory() as session:
        stmt = (
            pg_insert(Claim)
            .values(message_id=msg_id, agent_id="worker-test")
            .on_conflict_do_nothing(index_elements=["message_id"])
        )
        result = await session.execute(stmt)
        await session.commit()
        print(f"[BUG] rowcount = {result.rowcount}  (expected 1, likely 0)")
        return result.rowcount


async def test_fix_returning():
    """Fixed code — use .returning() and check fetchone()."""
    msg_id = uuid.uuid4()
    async with session_factory() as session:
        stmt = (
            pg_insert(Claim)
            .values(message_id=msg_id, agent_id="worker-test")
            .on_conflict_do_nothing(index_elements=["message_id"])
            .returning(Claim.id)
        )
        result = await session.execute(stmt)
        row = result.fetchone()
        await session.commit()
        claimed = row is not None
        print(f"[FIX] returning row = {row}, claimed = {claimed}  (expected True)")

        # try duplicate — should NOT claim
        async with session_factory() as session2:
            result2 = await session2.execute(stmt)
            row2 = result2.fetchone()
            await session2.commit()
            claimed2 = row2 is not None
            print(f"[FIX] duplicate: row = {row2}, claimed = {claimed2}  (expected False)")

        return claimed, not claimed2


async def main():
    await setup()

    print("=== Reproducing bug ===")
    rc = await test_bug_rowcount()

    print("\n=== Testing fix ===")
    first_ok, dup_ok = await test_fix_returning()

    print("\n=== Results ===")
    print(f"Bug reproduced (rowcount==0 on success): {rc == 0}")
    print(f"Fix works for new claim: {first_ok}")
    print(f"Fix correctly rejects duplicate: {dup_ok}")

    # cleanup
    async with session_factory() as s:
        await s.execute(text("DELETE FROM claims WHERE agent_id = 'worker-test'"))
        await s.commit()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
