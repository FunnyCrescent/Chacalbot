"""
MemoryStore — semantic 'world diary' for the D&D bot.

Adapted from the embedding-diary pattern (Diary/DiaryEmbedding/DiaryQueryAI/
sleepingConsolidation): every notable narrative beat, NPC fact, or lore snippet is
stored as a MemoryEntry with an embedding vector. Retrieval is by cosine similarity,
not keyword search. Periodically (or on demand), a "sleep" pass consolidates a
cluster of raw entries into fewer, higher-quality ones via an LLM, the same way
human memory compresses a day's experience during sleep.

This is intentionally dependency-light: no numpy, no external vector DB — cosine
similarity is done in pure Python, and vectors are stored as JSON text in SQLite.
At campaign scale (hundreds, not millions of entries) this is plenty fast.

Every public method fails SOFT: memory is an enhancement to the game, not a hard
dependency. If the embeddings endpoint is unreachable or quota'd out, the bot keeps
running — it just narrates without the extra recall for that turn.
"""

import json
import logging
import math
from typing import Dict, List, Optional

from libs.config_legacy import MEMORY_TOP_K, MEMORY_MIN_RAW_FOR_SLEEP, MEMORY_SLEEP_CLUSTER_SIZE
from libs.db import DatabaseManager, MemoryEntry

logger = logging.getLogger(__name__)


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


CONSOLIDATOR_PROMPT = """Ты — архивариус памяти для D&D-кампании. Тебе дают кучу разрозненных
"сырых" воспоминаний (кусков нарратива, фактов об NPC, событий). Твоя задача — как человеческий
сон, который консолидирует опыт дня: слить дублирующиеся/связанные записи в меньшее число более
плотных и полезных, выбросить незначительные детали, сохранить всё важное для будущих решений.

ПРАВИЛА:
1. Не выдумывай ничего, чего нет в исходных записях.
2. Если несколько записей об одном и том же — слей их в одну ёмкую запись.
3. Каждой итоговой записи присвой confidence: 1.0 если это точно установленный игровой факт
   (кто-то умер, квест завершён, локация существует), 0.0 если это скорее общее впечатление/тон,
   -1.0 если запись описывает то, что впоследствии оказалось ЛОЖЬЮ или было отменено в игре.
4. Не создавай больше записей, чем было на входе — цель именно СЖАТЬ.

Ответь СТРОГО JSON-массивом, без markdown, без пояснений:
[{"content": "текст воспоминания", "confidence": 1.0}, ...]"""


class MemoryStore:
    def __init__(self, db_manager: DatabaseManager, dm_engine):
        self.db_manager = db_manager
        self.dm = dm_engine

    # ═══════════════════════════════════════════════════════════
    # Write
    # ═══════════════════════════════════════════════════════════

    async def remember(self, session_id: str, content: str, source_type: str = "narrative",
                       confidence: float = 0.0) -> bool:
        """Store one memory entry with its embedding. Returns False (soft-fails) on any error."""
        content = (content or "").strip()
        if not content:
            return False
        try:
            vectors = await self.dm.embedder.embed([content])
            if not vectors:
                return False
            db = self.db_manager.get_db(session_id)
            db.add_memory_entry(MemoryEntry(
                session_id=session_id, content=content, source_type=source_type,
                embedding=json.dumps(vectors[0]), confidence=confidence, score=0.0,
            ))
            return True
        except Exception as e:
            logger.warning(f"[memory] remember() failed, continuing without it: {e}")
            return False

    # ═══════════════════════════════════════════════════════════
    # Read — cosine-similarity recall
    # ═══════════════════════════════════════════════════════════

    async def recall(self, session_id: str, query_text: str, top_k: int = None) -> List[MemoryEntry]:
        """Return the top_k most semantically relevant memory entries for query_text.
        Soft-fails to [] on any error (embeddings down, no entries yet, etc.)."""
        top_k = top_k or MEMORY_TOP_K
        try:
            db = self.db_manager.get_db(session_id)
            all_entries = db.get_memory_entries(session_id)
            if not all_entries:
                return []
            query_vec = (await self.dm.embedder.embed([query_text]))[0]

            scored = []
            for entry in all_entries:
                try:
                    vec = json.loads(entry.embedding)
                except Exception:
                    continue
                sim = _cosine(query_vec, vec)
                # ground-truth (confidence==1) and recently/frequently used entries get a small boost
                boosted = sim + (0.05 if entry.confidence >= 1.0 else 0) + min(entry.usage_count * 0.01, 0.05)
                scored.append((boosted, entry))

            scored.sort(key=lambda x: x[0], reverse=True)
            top = [e for score, e in scored[:top_k] if score > 0.15]
            if top:
                db.touch_memory_entries([e.id for e in top])
            return top
        except Exception as e:
            logger.warning(f"[memory] recall() failed, continuing without it: {e}")
            return []

    async def build_context_digest(self, session_id: str, query_text: str) -> str:
        """The 'doppelganger model' step: before the Master writes narrative, ask a
        cheap model to answer 'what do we know about this place/situation' using ONLY
        retrieved memory fragments (RAG-style, like the C++ example's queryAI). Returns
        a compact digest string, or "" if there's nothing relevant / on any failure."""
        try:
            entries = await self.recall(session_id, query_text, top_k=MEMORY_TOP_K)
            if not entries:
                return ""
            fragments = "\n".join(f"- [{e.source_type}] {e.content}" for e in entries)
            prompt = f"""Вот воспоминания мира, найденные по запросу "{query_text}":
{fragments}

Составь КОРОТКУЙ (3-6 предложений) фактический дайджест для Дунгеон Мастера: что важно помнить
прямо сейчас об этом месте/ситуации. Используй ТОЛЬКО то, что есть в воспоминаниях выше — если
там ничего релевантного нет, ответь пустой строкой. Не выдумывай, не добавляй ничего от себя."""
            response = await self.dm.memory.chat([{"role": "user", "content": prompt}], max_tokens=400)
            return response["choices"][0]["message"].get("content", "").strip()
        except Exception as e:
            logger.warning(f"[memory] build_context_digest() failed, continuing without it: {e}")
            return ""

    # ═══════════════════════════════════════════════════════════
    # Sleep consolidation
    # ═══════════════════════════════════════════════════════════

    async def maybe_sleep(self, session_id: str) -> bool:
        """Check whether it's time to consolidate, and do one round if so. Returns True
        if a consolidation pass actually ran."""
        try:
            db = self.db_manager.get_db(session_id)
            raw_count = db.count_raw_memory_entries(session_id)
            if raw_count < MEMORY_MIN_RAW_FOR_SLEEP:
                return False
            return await self.sleep_now(session_id)
        except Exception as e:
            logger.warning(f"[memory] maybe_sleep() check failed: {e}")
            return False

    async def sleep_now(self, session_id: str) -> bool:
        """Force one consolidation round regardless of threshold. Ground-truth entries
        (confidence >= 1.0) are left untouched — everything else in the oldest cluster
        gets merged/rewritten by the LLM and re-embedded."""
        try:
            db = self.db_manager.get_db(session_id)
            candidates = [e for e in db.get_memory_entries(session_id) if e.confidence < 1.0]
            if len(candidates) < 2:
                return False

            cluster = candidates[:MEMORY_SLEEP_CLUSTER_SIZE]
            raw_block = "\n".join(f"{i+1}. [{e.source_type}, confidence={e.confidence}] {e.content}"
                                  for i, e in enumerate(cluster))
            messages = [{"role": "user", "content": f"Сырые воспоминания на консолидацию:\n{raw_block}"}]
            response = await self.dm.memory.chat(messages, system_prompt=CONSOLIDATOR_PROMPT, max_tokens=2000)
            raw = response["choices"][0]["message"].get("content", "[]").strip()
            if raw.startswith("```"):
                raw = raw.strip("`")
                if raw.lower().startswith("json"):
                    raw = raw[4:]
            consolidated = json.loads(raw)
            if not isinstance(consolidated, list) or not consolidated:
                return False

            new_contents = [c.get("content", "") for c in consolidated if c.get("content")]
            if not new_contents:
                return False
            vectors = await self.dm.embedder.embed(new_contents)

            old_ids = [e.id for e in cluster]
            for c, vec in zip(consolidated, vectors):
                content = c.get("content", "")
                if not content:
                    continue
                confidence = float(c.get("confidence", 0.0))
                db.add_memory_entry(MemoryEntry(
                    session_id=session_id, content=content, source_type="consolidated",
                    embedding=json.dumps(vec), confidence=confidence, score=1.0,
                ))
            db.delete_memory_entries(old_ids)
            logger.info(f"[memory] sleep_now: session={session_id} consolidated {len(old_ids)} -> {len(new_contents)} entries")
            return True
        except Exception as e:
            logger.warning(f"[memory] sleep_now() failed, memory left as-is: {e}")
            return False
