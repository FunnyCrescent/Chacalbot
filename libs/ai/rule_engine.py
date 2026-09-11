"""Embedding-based rule validation engine for D&D 5e bot.

Loads rule .md files from a directory, chunks them into sections,
pre-computes embeddings for semantic similarity search, and provides
both embedding-based and keyword-based rule matching.

Typical usage::

    from libs.ai.rule_engine import RuleEngine
    from libs.ai.client import OpenAIClient
    from libs.config_legacy import EMBEDDING_MODEL

    client = OpenAIClient(model=EMBEDDING_MODEL)
    engine = RuleEngine(embedding_client=client, rules_dir="libs/rules")
    await engine.initialize()  # loads rules + computes embeddings

    result = engine.validate_action("spawn merchant at dungeon_room")
    if not result.is_valid:
        for v in result.violations:
            print(v)
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# Data classes
# ═══════════════════════════════════════════════════════════════

@dataclass
class RuleChunk:
    """A single chunk of rule text (one section from a .md file)."""
    rule_id: str           # e.g. "combat#action_economy"
    file_name: str         # e.g. "combat.md"
    section_title: str     # e.g. "Action Economy"
    text: str              # Full text of the section
    keywords: List[str] = field(default_factory=list)  # Extracted keywords
    embedding: Optional[List[float]] = None             # Embedding vector (lazy)
    line_start: int = 0    # Line number in source file
    line_end: int = 0      # Line number in source file

    @property
    def file_stem(self) -> str:
        return Path(self.file_name).stem


@dataclass
class RuleMatch:
    """A rule chunk matched against a query, with similarity score."""
    rule_id: str
    section_title: str
    text: str
    similarity_score: float    # cosine similarity, 0.0 to 1.0
    match_method: str = "embedding"  # "embedding" | "keyword"


@dataclass
class RuleCheckResult:
    """Result of validating an action against the rules."""
    is_valid: bool
    relevant_rules: List[RuleMatch] = field(default_factory=list)
    violations: List[str] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    query: str = ""
    confidence: float = 0.0  # How confident the engine is in the result (0-1)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "relevant_rules": [
                {"rule_id": m.rule_id, "section_title": m.section_title,
                 "text": m.text[:200], "similarity_score": m.similarity_score,
                 "match_method": m.match_method}
                for m in self.relevant_rules
            ],
            "violations": self.violations,
            "suggestions": self.suggestions,
            "query": self.query,
            "confidence": self.confidence,
        }


# ═══════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════

# Similarity thresholds
EMBEDDING_MATCH_THRESHOLD = 0.35   # Minimum cosine similarity for embedding match
KEYWORD_MATCH_THRESHOLD = 0.05     # Minimum score for keyword match (coverage-based, low is OK)
VALIDATION_VIOLATION_THRESHOLD = 0.55  # Similarity at which a rule is considered relevant for violations

# Keywords that indicate violations / forbidden actions in rule text
VIOLATION_INDICATORS = {
    "forbidden", "prohibited", "cannot", "must not", "not allowed",
    "never", "impossible", "rejected", "block", "zero tolerance",
    "is_flag", "violation", "cheat", "exploit", "invalid",
}

# Keywords that suggest allowed / valid actions
ALLOW_INDICATORS = {
    "allowed", "permitted", "can", "may", "possible", "valid",
    "approved", "optional", "variant", "discretion",
}

# NPC spawn location mapping — extracted from npc_spawn.md rules
NPC_LOCATION_RULES: Dict[str, Dict[str, Any]] = {
    "merchant": {
        "allowed_locations": {"market", "town_square", "bazaar", "shop", "trading_post"},
        "forbidden_locations": {"dungeon", "wilderness", "road", "cave", "ruins"},
        "wandering_probability": 0.10,
        "wandering_locations": {"road", "crossroads", "village_edge"},
    },
    "trader": {
        "allowed_locations": {"market", "town_square", "bazaar", "shop", "trading_post"},
        "forbidden_locations": {"dungeon", "wilderness", "road", "cave", "ruins"},
        "wandering_probability": 0.10,
        "wandering_locations": {"road", "crossroads", "village_edge"},
    },
    "shopkeeper": {
        "allowed_locations": {"shop", "market", "town_square"},
        "forbidden_locations": {"dungeon", "wilderness", "road", "cave", "ruins"},
        "wandering_probability": 0.0,
        "wandering_locations": set(),
    },
    "innkeeper": {
        "allowed_locations": {"inn", "tavern", "waystation"},
        "forbidden_locations": {"wilderness", "dungeon", "road"},
        "wandering_probability": 0.0,
        "wandering_locations": set(),
    },
    "guard": {
        "allowed_locations": {"gate", "barracks", "patrol_route", "town_square"},
        "forbidden_locations": {"wilderness"},
        "wandering_probability": 0.25,
        "wandering_locations": {"road", "gate"},
    },
    "blacksmith": {
        "allowed_locations": {"forge", "workshop", "market", "town_square"},
        "forbidden_locations": {"wilderness", "dungeon"},
        "wandering_probability": 0.0,
        "wandering_locations": set(),
    },
    "priest": {
        "allowed_locations": {"temple", "shrine", "cathedral", "cathedral"},
        "forbidden_locations": set(),
        "wandering_probability": 0.0,
        "wandering_locations": set(),
    },
    "noble": {
        "allowed_locations": {"manor", "palace", "courthouse", "town_square"},
        "forbidden_locations": {"wilderness", "dungeon", "road"},
        "wandering_probability": 0.0,
        "wandering_locations": set(),
    },
    "sage": {
        "allowed_locations": {"library", "academy", "tower"},
        "forbidden_locations": {"wilderness", "dungeon"},
        "wandering_probability": 0.0,
        "wandering_locations": set(),
    },
}


# ═══════════════════════════════════════════════════════════════
# Utility functions
# ═══════════════════════════════════════════════════════════════

def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Compute cosine similarity between two vectors."""
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


def _extract_keywords(text: str) -> List[str]:
    """Extract meaningful keywords from text (lowercase, deduplicated)."""
    # Remove markdown formatting
    clean = re.sub(r'[#*`\[\]()]', ' ', text)
    # Split into words, lowercase, filter short/stop words
    stop_words = {
        "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
        "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
        "being", "have", "has", "had", "do", "does", "did", "will", "would",
        "shall", "should", "may", "might", "must", "can", "could", "not",
        "no", "nor", "if", "then", "else", "when", "where", "which", "who",
        "whom", "this", "that", "these", "those", "it", "its", "as", "than",
        "each", "every", "all", "both", "any", "some", "such", "other",
        "more", "most", "less", "least", "very", "much", "many", "few",
        "only", "just", "also", "even", "still", "already", "yet", "so",
        "up", "down", "out", "off", "over", "under", "again", "further",
        "once", "here", "there", "how", "what", "why", "per", "see",
        "e", "g", "vs", "etc", "ie", "p",
    }
    words = re.findall(r'[a-zA-Z_]+', clean.lower())
    keywords = []
    seen = set()
    for w in words:
        if len(w) >= 3 and w not in stop_words and w not in seen:
            keywords.append(w)
            seen.add(w)
    return keywords


def _keyword_score(query_keywords: List[str], chunk_keywords: List[str]) -> float:
    """Compute a keyword overlap score based on query coverage.

    Uses a weighted combination of:
    - Query coverage: what fraction of query keywords appear in the chunk.
    - Chunk specificity: bonus for chunks that are more focused on the query terms.

    This is more useful than Jaccard for retrieval, where we care about how many
    of the user's terms are found, not penalizing the chunk for having many other terms.
    """
    if not query_keywords or not chunk_keywords:
        return 0.0
    query_set = set(query_keywords)
    chunk_set = set(chunk_keywords)
    intersection = query_set & chunk_set
    if not intersection:
        return 0.0
    # Query coverage: what fraction of the query terms are in this chunk
    coverage = len(intersection) / len(query_set)
    # Specificity bonus: what fraction of the chunk is about the query terms
    specificity = len(intersection) / len(chunk_set)
    # Weighted combination: coverage is more important (the user's terms matter most)
    # but specificity helps rank chunks that are more focused on the topic
    score = 0.7 * coverage + 0.3 * specificity
    return score


def _parse_npc_spawn_action(action_description: str) -> Optional[Dict[str, str]]:
    """Try to parse an NPC spawn action from the description.
    Returns dict with npc_type and location_type, or None if not a spawn action.
    """
    # Patterns like "spawn merchant at dungeon", "create npc trader in wilderness"
    # Also "merchant at dungeon_room", "place shopkeeper in cave"
    patterns = [
        r'(?:spawn|create|place|put|add)\s+(?:npc\s+)?(\w+)\s+(?:at|in|to|near)\s+(\w+)',
        r'(\w+)\s+(?:at|in|to|near)\s+(\w+)',
    ]
    action_lower = action_description.lower().strip()

    for pattern in patterns:
        match = re.search(pattern, action_lower)
        if match:
            npc_type = match.group(1)
            location_type = match.group(2)
            # Validate that the npc_type is one we know
            if npc_type in NPC_LOCATION_RULES:
                return {"npc_type": npc_type, "location_type": location_type}

    return None


# ═══════════════════════════════════════════════════════════════
# RuleEngine
# ═══════════════════════════════════════════════════════════════

class RuleEngine:
    """Embedding-based rule validation engine.

    Loads rule .md files, chunks them into sections, computes embeddings,
    and provides both semantic and keyword-based rule matching.
    """

    def __init__(self, embedding_client=None, rules_dir: str = ""):
        """
        Args:
            embedding_client: An OpenAIClient instance configured for embeddings.
                              If None, only keyword-based matching is available.
            rules_dir: Path to the directory containing rule .md files.
        """
        self.embedding_client = embedding_client
        self.rules_dir = Path(rules_dir) if rules_dir else Path(__file__).resolve().parent.parent / "rules"
        self.chunks: List[RuleChunk] = []
        self._embeddings_matrix: Optional[np.ndarray] = None
        self._embeddings_computed = False
        self._initialized = False
        self._last_load_time: float = 0.0

    # ─── Initialization ────────────────────────────────────────

    async def initialize(self) -> None:
        """Load rules and compute embeddings. Call this once at startup."""
        self.load_rules()
        await self.compute_embeddings()
        self._initialized = True
        logger.info(
            f"[RuleEngine] Initialized: {len(self.chunks)} chunks loaded, "
            f"embeddings={'computed' if self._embeddings_computed else 'not computed'}"
        )

    # ─── Rule Loading ──────────────────────────────────────────

    def load_rules(self) -> None:
        """Read all .md files from rules_dir and chunk them into sections."""
        self.chunks.clear()
        self._embeddings_computed = False
        self._embeddings_matrix = None

        if not self.rules_dir.exists():
            logger.warning(f"[RuleEngine] Rules directory not found: {self.rules_dir}")
            return

        md_files = sorted(self.rules_dir.glob("*.md"))
        if not md_files:
            logger.warning(f"[RuleEngine] No .md files found in {self.rules_dir}")
            return

        for md_file in md_files:
            try:
                file_chunks = self._parse_markdown_file(md_file)
                self.chunks.extend(file_chunks)
                logger.debug(f"[RuleEngine] Loaded {len(file_chunks)} chunks from {md_file.name}")
            except Exception as e:
                logger.error(f"[RuleEngine] Error loading {md_file.name}: {e}")

        self._last_load_time = time.time()
        logger.info(f"[RuleEngine] Loaded {len(self.chunks)} rule chunks from {len(md_files)} files")

    def _parse_markdown_file(self, filepath: Path) -> List[RuleChunk]:
        """Parse a single .md file into RuleChunks, splitting on ## headers."""
        content = filepath.read_text(encoding="utf-8")
        lines = content.split("\n")
        file_name = filepath.name
        file_stem = filepath.stem

        # Find all ## headers (level 2+ sections)
        sections: List[Tuple[str, int, int]] = []  # (title, start_line, end_line)

        current_title = file_stem.replace("_", " ").title()  # File name as top-level
        current_start = 0

        for i, line in enumerate(lines):
            # Match ## or ### headers
            header_match = re.match(r'^(#{2,3})\s+(.+)$', line)
            if header_match:
                # Save previous section
                if i > current_start:
                    sections.append((current_title, current_start, i - 1))
                current_title = header_match.group(2).strip()
                current_start = i

        # Save last section
        if len(lines) > current_start:
            sections.append((current_title, current_start, len(lines) - 1))

        # Build chunks
        chunks: List[RuleChunk] = []
        for idx, (title, start, end) in enumerate(sections):
            section_text = "\n".join(lines[start:end + 1]).strip()
            if not section_text or len(section_text) < 20:
                continue  # Skip empty or trivially small sections

            # Create a rule_id: file_stem#section_title_slug
            slug = re.sub(r'[^a-zA-Z0-9]+', '_', title.lower()).strip('_')
            rule_id = f"{file_stem}#{slug}"

            keywords = _extract_keywords(section_text)

            chunk = RuleChunk(
                rule_id=rule_id,
                file_name=file_name,
                section_title=title,
                text=section_text,
                keywords=keywords,
                line_start=start + 1,  # 1-indexed
                line_end=end + 1,
            )
            chunks.append(chunk)

        # If no sections were found (no ## headers), treat entire file as one chunk
        if not chunks and content.strip():
            keywords = _extract_keywords(content)
            chunk = RuleChunk(
                rule_id=f"{file_stem}#all",
                file_name=file_name,
                section_title=file_stem.replace("_", " ").title(),
                text=content.strip(),
                keywords=keywords,
                line_start=1,
                line_end=len(lines),
            )
            chunks.append(chunk)

        return chunks

    # ─── Embedding Computation ─────────────────────────────────

    async def compute_embeddings(self) -> None:
        """Compute embeddings for all rule chunks using the embedding client.

        This is done lazily — if the client is None, embeddings are skipped
        and only keyword matching is available.
        """
        if not self.embedding_client:
            logger.info("[RuleEngine] No embedding client — keyword-only mode")
            return

        if not self.chunks:
            logger.warning("[RuleEngine] No chunks to embed")
            return

        # Compute embeddings in batches (API limit is typically 2048 inputs)
        BATCH_SIZE = 64
        texts = [chunk.text for chunk in self.chunks]
        all_embeddings: List[List[float]] = []

        for i in range(0, len(texts), BATCH_SIZE):
            batch = texts[i:i + BATCH_SIZE]
            try:
                batch_embeddings = await self.embedding_client.embed(batch)
                all_embeddings.extend(batch_embeddings)
                logger.debug(f"[RuleEngine] Embedded batch {i//BATCH_SIZE + 1}: {len(batch)} texts")
            except Exception as e:
                logger.error(f"[RuleEngine] Embedding batch {i//BATCH_SIZE + 1} failed: {e}")
                # Fill with None for failed batch
                all_embeddings.extend([None] * len(batch))

        # Assign embeddings to chunks and build matrix
        valid_embeddings: List[List[float]] = []
        for chunk, emb in zip(self.chunks, all_embeddings):
            chunk.embedding = emb
            if emb is not None:
                valid_embeddings.append(emb)

        if valid_embeddings:
            self._embeddings_matrix = np.array(valid_embeddings, dtype=np.float32)
            self._embeddings_computed = True
            logger.info(
                f"[RuleEngine] Embeddings computed: {len(valid_embeddings)}/{len(self.chunks)} chunks"
            )
        else:
            logger.warning("[RuleEngine] No embeddings computed — keyword-only mode")

    def _get_chunk_embedding(self, chunk: RuleChunk) -> Optional[np.ndarray]:
        """Get the embedding vector for a chunk as a numpy array."""
        if chunk.embedding is None:
            return None
        return np.array(chunk.embedding, dtype=np.float32)

    # ─── Rule Search ───────────────────────────────────────────

    async def find_relevant_rules(
        self,
        query: str,
        top_k: int = 3,
        method: str = "auto",
    ) -> List[RuleMatch]:
        """Find rules relevant to a query.

        Args:
            query: The search query (e.g., "can a merchant appear in a dungeon").
            top_k: Number of top results to return.
            method: "embedding" (semantic), "keyword" (exact), or "auto" (try embedding first, fallback to keyword).

        Returns:
            List of RuleMatch objects sorted by similarity score (descending).
        """
        if not self.chunks:
            return []

        query_keywords = _extract_keywords(query)

        # Try embedding-based search first
        embedding_matches: List[RuleMatch] = []
        if method in ("embedding", "auto") and self._embeddings_computed and self.embedding_client:
            embedding_matches = await self._find_by_embedding(query, top_k)

        # Keyword-based search
        keyword_matches: List[RuleMatch] = []
        if method in ("keyword", "auto"):
            keyword_matches = self._find_by_keyword(query_keywords, top_k)

        # Merge results
        if method == "auto":
            # Combine both methods, embedding results take priority
            combined: Dict[str, RuleMatch] = {}

            # Add keyword matches first (lower priority)
            for m in keyword_matches:
                combined[m.rule_id] = m

            # Override with embedding matches (higher priority)
            for m in embedding_matches:
                if m.rule_id in combined:
                    # Keep the higher score
                    existing = combined[m.rule_id]
                    if m.similarity_score > existing.similarity_score:
                        combined[m.rule_id] = m
                else:
                    combined[m.rule_id] = m

            # Sort by score, take top_k
            results = sorted(combined.values(), key=lambda x: x.similarity_score, reverse=True)
            return results[:top_k]

        # Single method
        if method == "embedding":
            return embedding_matches[:top_k]
        return keyword_matches[:top_k]

    async def _find_by_embedding(self, query: str, top_k: int) -> List[RuleMatch]:
        """Find rules using embedding similarity."""
        try:
            query_embeddings = await self.embedding_client.embed([query])
            if not query_embeddings:
                return []
            query_vec = np.array(query_embeddings[0], dtype=np.float32)
        except Exception as e:
            logger.error(f"[RuleEngine] Failed to embed query: {e}")
            return []

        # Compute similarity against all chunk embeddings
        matches: List[RuleMatch] = []
        for chunk in self.chunks:
            if chunk.embedding is None:
                continue
            chunk_vec = np.array(chunk.embedding, dtype=np.float32)
            score = _cosine_similarity(query_vec, chunk_vec)
            if score >= EMBEDDING_MATCH_THRESHOLD:
                matches.append(RuleMatch(
                    rule_id=chunk.rule_id,
                    section_title=chunk.section_title,
                    text=chunk.text,
                    similarity_score=score,
                    match_method="embedding",
                ))

        matches.sort(key=lambda x: x.similarity_score, reverse=True)
        return matches[:top_k]

    def _find_by_keyword(self, query_keywords: List[str], top_k: int) -> List[RuleMatch]:
        """Find rules using keyword overlap."""
        matches: List[RuleMatch] = []
        for chunk in self.chunks:
            score = _keyword_score(query_keywords, chunk.keywords)
            if score >= KEYWORD_MATCH_THRESHOLD:
                matches.append(RuleMatch(
                    rule_id=chunk.rule_id,
                    section_title=chunk.section_title,
                    text=chunk.text,
                    similarity_score=score,
                    match_method="keyword",
                ))

        matches.sort(key=lambda x: x.similarity_score, reverse=True)
        return matches[:top_k]

    # ─── Action Validation ─────────────────────────────────────

    async def validate_action(self, action_description: str) -> RuleCheckResult:
        """Check if an action is valid per the loaded rules.

        This method:
        1. Checks for structured rule violations (e.g., NPC spawn location binding).
        2. Searches for semantically relevant rules.
        3. Analyzes relevant rules for violation/allow indicators.
        4. Returns a RuleCheckResult with is_valid, violations, and suggestions.

        Args:
            action_description: Natural language description of the action to validate.

        Returns:
            RuleCheckResult with validation outcome.
        """
        result = RuleCheckResult(
            is_valid=True,
            relevant_rules=[],
            violations=[],
            suggestions=[],
            query=action_description,
            confidence=0.0,
        )

        # Step 1: Check structured rules (NPC spawn location binding)
        structured_result = self._check_structured_rules(action_description)
        if structured_result:
            result.is_valid = structured_result.is_valid
            result.violations.extend(structured_result.violations)
            result.suggestions.extend(structured_result.suggestions)
            result.relevant_rules.extend(structured_result.relevant_rules)
            result.confidence = max(result.confidence, structured_result.confidence)
            if not structured_result.is_valid:
                # Structured rule violation is definitive — return early
                # But still add semantic matches for context
                pass

        # Step 2: Search for semantically relevant rules
        try:
            relevant = await self.find_relevant_rules(action_description, top_k=5)
        except Exception as e:
            logger.error(f"[RuleEngine] find_relevant_rules error: {e}")
            relevant = []

        # Add only rules not already in the result
        existing_ids = {m.rule_id for m in result.relevant_rules}
        for match in relevant:
            if match.rule_id not in existing_ids:
                result.relevant_rules.append(match)
                existing_ids.add(match.rule_id)

        # Step 3: Analyze relevant rules for violation indicators
        action_lower = action_description.lower()
        for match in relevant:
            if match.similarity_score < VALIDATION_VIOLATION_THRESHOLD:
                continue  # Not similar enough to matter

            text_lower = match.text.lower()

            # Check if the rule text contains violation indicators
            has_violation = any(ind in text_lower for ind in VIOLATION_INDICATORS)
            has_allow = any(ind in text_lower for ind in ALLOW_INDICATORS)

            if has_violation and not has_allow:
                # The rule seems to forbid something related to this action
                # Extract the specific violation text
                violation_text = self._extract_violation_context(match.text, action_lower)
                if violation_text and violation_text not in result.violations:
                    result.violations.append(violation_text)
                    # Lower confidence if we found violations but structured check said OK
                    if result.is_valid and not structured_result:
                        result.is_valid = False
                        result.confidence = match.similarity_score

            # Check for suggestions (rules that describe alternatives)
            suggestion = self._extract_suggestion(match.text, action_lower)
            if suggestion and suggestion not in result.suggestions:
                result.suggestions.append(suggestion)

        # Step 4: Calculate overall confidence
        if result.violations and result.is_valid:
            # Contradiction: violations found but not definitive — lower confidence
            result.confidence = max(0.3, result.confidence - 0.2)
        elif not result.violations and result.is_valid:
            # No violations found — moderate confidence (we can't prove a negative)
            result.confidence = max(result.confidence, 0.5)
        elif result.violations and not result.is_valid:
            # Violations found and action is invalid — high confidence
            result.confidence = max(result.confidence, 0.8)

        return result

    def _check_structured_rules(self, action_description: str) -> Optional[RuleCheckResult]:
        """Check action against structured rules (NPC spawn location binding, etc.).

        Returns None if no structured rule applies, or a RuleCheckResult if one does.
        """
        # NPC spawn location check
        spawn_info = _parse_npc_spawn_action(action_description)
        if spawn_info:
            return self._validate_npc_spawn(
                spawn_info["npc_type"],
                spawn_info["location_type"],
            )

        # Add more structured rule checks here as needed
        return None

    def _validate_npc_spawn(self, npc_type: str, location_type: str) -> RuleCheckResult:
        """Validate an NPC spawn against location-binding rules."""
        rules = NPC_LOCATION_RULES.get(npc_type)
        if not rules:
            # Unknown NPC type — allow but with low confidence
            return RuleCheckResult(
                is_valid=True,
                violations=[],
                suggestions=[f"Unknown NPC type '{npc_type}' — no location rules defined. Proceeding with DM discretion."],
                relevant_rules=[],
                confidence=0.3,
            )

        # Clean location type (e.g., "dungeon_room" -> "dungeon")
        location_base = location_type.split("_")[0] if "_" in location_type else location_type

        allowed = rules["allowed_locations"]
        forbidden = rules["forbidden_locations"]
        wandering_locs = rules.get("wandering_locations", set())
        wandering_prob = rules.get("wandering_probability", 0.0)

        # Find the matching rule chunk for context
        npc_rule_match: List[RuleMatch] = []
        for chunk in self.chunks:
            if chunk.file_stem == "npc_spawn":
                npc_rule_match.append(RuleMatch(
                    rule_id=chunk.rule_id,
                    section_title=chunk.section_title,
                    text=chunk.text,
                    similarity_score=1.0,
                    match_method="structured",
                ))

        # Check if location is explicitly forbidden
        if location_base in forbidden or location_type in forbidden:
            return RuleCheckResult(
                is_valid=False,
                violations=[
                    f"NPC type '{npc_type}' CANNOT be spawned at location type '{location_type}'. "
                    f"Forbidden location types: {', '.join(sorted(forbidden))}. "
                    f"This violates the NPC Location Binding rule (Rule 1 in npc_spawn.md)."
                ],
                suggestions=[
                    f"Place the {npc_type} at one of these allowed location types: "
                    f"{', '.join(sorted(allowed))}.",
                    f"The nearest valid location should be used instead.",
                ],
                relevant_rules=npc_rule_match[:2],
                confidence=0.95,
            )

        # Check if location is explicitly allowed
        if location_base in allowed or location_type in allowed:
            return RuleCheckResult(
                is_valid=True,
                violations=[],
                suggestions=[],
                relevant_rules=npc_rule_match[:2],
                confidence=0.95,
            )

        # Check if it's a wandering NPC location
        if location_base in wandering_locs or location_type in wandering_locs:
            if wandering_prob > 0:
                return RuleCheckResult(
                    is_valid=True,
                    violations=[],
                    suggestions=[
                        f"Wandering {npc_type} at '{location_type}' is allowed but rare "
                        f"({wandering_prob:.0%} probability per travel segment). "
                        f"The {npc_type} must have a named origin and destination.",
                    ],
                    relevant_rules=npc_rule_match[:2],
                    confidence=0.85,
                )
            else:
                return RuleCheckResult(
                    is_valid=False,
                    violations=[
                        f"NPC type '{npc_type}' cannot wander — it is a fixed-location NPC. "
                        f"Must be placed at: {', '.join(sorted(allowed))}."
                    ],
                    suggestions=[
                        f"Place the {npc_type} at one of these allowed location types: "
                        f"{', '.join(sorted(allowed))}.",
                    ],
                    relevant_rules=npc_rule_match[:2],
                    confidence=0.90,
                )

        # Location is not in any list — DM discretion
        return RuleCheckResult(
            is_valid=True,
            violations=[],
            suggestions=[
                f"Location type '{location_type}' is not explicitly allowed or forbidden for "
                f"'{npc_type}'. DM discretion applies. Allowed: {', '.join(sorted(allowed))}. "
                f"Forbidden: {', '.join(sorted(forbidden))}.",
            ],
            relevant_rules=npc_rule_match[:2],
            confidence=0.5,
        )

    def _extract_violation_context(self, rule_text: str, action_lower: str) -> Optional[str]:
        """Extract the most relevant violation context from a rule section."""
        # Find sentences that contain both action keywords and violation indicators
        sentences = re.split(r'[.!?\n]+', rule_text)
        action_words = set(_extract_keywords(action_lower))
        best_sentence = None
        best_overlap = 0

        for sentence in sentences:
            sentence_lower = sentence.lower()
            has_violation = any(ind in sentence_lower for ind in VIOLATION_INDICATORS)
            if not has_violation:
                continue

            sentence_words = set(_extract_keywords(sentence))
            overlap = len(action_words & sentence_words)
            if overlap > best_overlap:
                best_overlap = overlap
                best_sentence = sentence.strip()

        if best_sentence and best_overlap >= 1:
            return best_sentence[:300]  # Truncate long sentences
        return None

    def _extract_suggestion(self, rule_text: str, action_lower: str) -> Optional[str]:
        """Extract a suggestion or alternative from rule text."""
        # Look for phrases like "instead", "alternative", "may also", "can also"
        suggestion_patterns = [
            r'(?:instead|alternative|may also|can also|instead of)[^.!?]*[.!?]',
            r'(?:place|spawn|put|create)\s+\w+\s+(?:at|in)\s+[^.!?]*[.!?]',
        ]
        for pattern in suggestion_patterns:
            match = re.search(pattern, rule_text, re.IGNORECASE)
            if match:
                return match.group(0).strip()[:300]
        return None

    # ─── Direct Rule Access ────────────────────────────────────

    def get_rule(self, rule_id: str) -> Optional[str]:
        """Get the full text of a specific rule chunk by rule_id.

        Args:
            rule_id: The rule identifier (e.g., "combat#action_economy").

        Returns:
            The full text of the rule chunk, or None if not found.
        """
        for chunk in self.chunks:
            if chunk.rule_id == rule_id:
                return chunk.text
        return None

    def get_rules_by_file(self, file_name: str) -> List[RuleChunk]:
        """Get all rule chunks from a specific .md file.

        Args:
            file_name: The file name (e.g., "combat.md") or stem (e.g., "combat").

        Returns:
            List of RuleChunks from that file.
        """
        stem = Path(file_name).stem
        return [c for c in self.chunks if c.file_stem == stem]

    def list_rules(self) -> List[Dict[str, Any]]:
        """List all available rules with metadata.

        Returns:
            List of dicts with rule_id, file_name, section_title, text_length, has_embedding.
        """
        return [
            {
                "rule_id": c.rule_id,
                "file_name": c.file_name,
                "section_title": c.section_title,
                "text_length": len(c.text),
                "has_embedding": c.embedding is not None,
                "keywords_count": len(c.keywords),
            }
            for c in self.chunks
        ]

    # ─── Hot Reload ────────────────────────────────────────────

    async def reload_rules(self) -> None:
        """Hot-reload rules from .md files and recompute embeddings."""
        logger.info("[RuleEngine] Hot-reloading rules...")
        self.load_rules()
        await self.compute_embeddings()
        logger.info(
            f"[RuleEngine] Reloaded: {len(self.chunks)} chunks, "
            f"embeddings={'computed' if self._embeddings_computed else 'not computed'}"
        )

    # ─── Statistics ────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        """Return engine statistics."""
        return {
            "total_chunks": len(self.chunks),
            "files": sorted(set(c.file_name for c in self.chunks)),
            "embeddings_computed": self._embeddings_computed,
            "embedding_count": sum(1 for c in self.chunks if c.embedding is not None),
            "last_load_time": self._last_load_time,
            "initialized": self._initialized,
            "rules_dir": str(self.rules_dir),
        }
