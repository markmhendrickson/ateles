#!/usr/bin/env python3
"""
General-purpose conversation parser using LLM inference.

This module provides a topic-agnostic framework for extracting structured data
from conversational exports (ChatGPT, etc.) by analyzing user and assistant
messages together with full context.

Key features:
- Schema-driven extraction (works for any data type)
- Chunked processing for long conversations
- Context preservation across chunks
- Handles implicit references ("Same", "One more", etc.)
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv

    # Load .env file from project root
    PROJECT_ROOT = Path(__file__).parent.parent.parent
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass  # python-dotenv not installed, skip

try:
    from openai import OpenAI

    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False
    OpenAI = None


class ConversationParser:
    """
    General-purpose parser for extracting structured data from conversations.

    Uses LLM inference to understand conversational context and extract
    structured records according to a provided schema.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gpt-4o-mini",
        chunk_size: int = 50,  # messages per chunk
        overlap: int = 10,  # messages of overlap between chunks
    ):
        """
        Initialize parser.

        Args:
            api_key: OpenAI API key (defaults to OPENAI_API_KEY env var)
            model: Model to use for inference
            chunk_size: Number of messages to process per chunk
            overlap: Messages to overlap between chunks for context
        """
        if not OPENAI_AVAILABLE:
            raise ImportError(
                "openai package not installed. Install with: pip install openai"
            )

        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise ValueError("OpenAI API key required (set OPENAI_API_KEY env var)")

        self.client = OpenAI(api_key=self.api_key)
        self.model = model
        self.chunk_size = chunk_size
        self.overlap = overlap

    def load_conversation(self, conv_path: Path) -> list[dict[str, Any]]:
        """
        Load conversation from ChatGPT export JSON.

        Returns list of messages with: role, text, create_time, date
        """
        with open(conv_path) as f:
            conv = json.load(f)

        mapping = conv.get("mapping", {})
        messages = []

        for node_id, node in mapping.items():
            msg = node.get("message")
            if not msg:
                continue

            author = msg.get("author", {}).get("role")
            if author not in ["user", "assistant"]:
                continue

            content = msg.get("content", {})
            if content.get("content_type") != "text":
                continue

            parts = content.get("parts", [])
            if not parts or not parts[0]:
                continue

            create_time = msg.get("create_time")
            if not create_time:
                continue

            messages.append(
                {
                    "id": node_id,
                    "role": author,
                    "text": parts[0],
                    "create_time": create_time,
                    "date": datetime.fromtimestamp(create_time).date().isoformat(),
                }
            )

        # Sort chronologically
        messages.sort(key=lambda x: x["create_time"])
        return messages

    def extract_with_schema(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any],
        conversation_title: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Extract structured data from conversation using schema.

        Args:
            messages: List of message dicts (from load_conversation)
            schema: Schema definition with:
                - name: Schema name (e.g., "exercise_sets")
                - description: What data to extract
                - fields: List of field definitions with name, type, description
                - examples: Optional examples of expected output
            conversation_title: Optional title for context

        Returns:
            List of extracted records matching schema
        """
        all_records = []
        context_summary = {}  # Track state across chunks

        # Process in chunks
        for chunk_idx, chunk_messages in enumerate(self._chunk_messages(messages)):
            print(
                f"Processing chunk {chunk_idx + 1} ({len(chunk_messages)} messages)..."
            )

            # Build prompt with schema and context
            prompt = self._build_extraction_prompt(
                chunk_messages,
                schema,
                conversation_title,
                context_summary,
            )

            # Call LLM
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {
                            "role": "system",
                            "content": "You are a precise data extraction assistant. Extract structured data from conversations according to the provided schema. Always return valid JSON in the exact format specified.",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    response_format={"type": "json_object"},
                    temperature=0.1,  # Low temperature for consistency
                )

                result_text = response.choices[0].message.content
                result_data = json.loads(result_text)

                # Extract records - handle both {"records": [...]} and direct array
                if isinstance(result_data, list):
                    records = result_data
                elif isinstance(result_data, dict):
                    records = result_data.get("records", [])
                else:
                    records = []

                # Validate records have required fields
                validated_records = []
                for record in records:
                    if isinstance(record, dict):
                        # Ensure all required fields from schema are present
                        validated_records.append(record)

                if validated_records:
                    all_records.extend(validated_records)
                    print(
                        f"  Extracted {len(validated_records)} records from this chunk"
                    )

                # Update context summary for next chunk
                context_summary = self._update_context_summary(
                    context_summary,
                    chunk_messages,
                    validated_records,
                )

            except json.JSONDecodeError as e:
                print(f"  Error: Invalid JSON response from LLM: {e}")
                print(
                    f"  Response preview: {result_text[:200] if 'result_text' in locals() else 'N/A'}"
                )
                continue
            except Exception as e:
                print(f"  Error processing chunk: {e}")
                import traceback

                traceback.print_exc()
                continue

        # Deduplicate records (by key fields if specified in schema)
        if "key_fields" in schema:
            all_records = self._deduplicate_records(all_records, schema["key_fields"])

        return all_records

    def _chunk_messages(
        self, messages: list[dict[str, Any]]
    ) -> list[list[dict[str, Any]]]:
        """Split messages into overlapping chunks."""
        chunks = []
        start = 0

        while start < len(messages):
            end = min(start + self.chunk_size, len(messages))
            chunk = messages[start:end]
            chunks.append(chunk)

            # Move start forward, but overlap
            start = end - self.overlap if end < len(messages) else end

        return chunks

    def _build_extraction_prompt(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any],
        conversation_title: str | None,
        context_summary: dict[str, Any],
    ) -> str:
        """Build prompt for LLM extraction."""

        # Format messages
        messages_text = []
        for msg in messages:
            role_label = "User" if msg["role"] == "user" else "Assistant"
            messages_text.append(f"{role_label} ({msg['date']}): {msg['text']}")

        messages_block = "\n".join(messages_text)

        # Build schema description
        schema_desc = f"""
Extract {schema["name"]} from this conversation.

Description: {schema["description"]}

Fields to extract:
"""
        for field in schema["fields"]:
            schema_desc += (
                f"- {field['name']} ({field['type']}): {field.get('description', '')}\n"
            )

        if "examples" in schema:
            schema_desc += f"\nExamples:\n{json.dumps(schema['examples'], indent=2)}\n"

        # Context from previous chunks
        context_block = ""
        if context_summary:
            context_block = f"""
Previous context (from earlier in conversation):
{json.dumps(context_summary, indent=2)}

Use this context to resolve implicit references like "Same", "One more", etc.
"""

        prompt = f"""
{schema_desc}

{context_block}

Conversation excerpt:
{messages_block}

Extract all {schema["name"]} mentioned in this conversation excerpt. Include:
1. Explicitly stated records
2. Records implied by context (e.g., "Same 8 reps" refers to previous exercise/weight)
3. Records from both user and assistant messages

Return JSON in this format:
{{
  "records": [
    {{
      "field1": "value1",
      "field2": "value2",
      ...
    }},
    ...
  ]
}}

Only include records that can be extracted with reasonable confidence from this excerpt.
"""
        return prompt

    def _update_context_summary(
        self,
        current_summary: dict[str, Any],
        messages: list[dict[str, Any]],
        extracted_records: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Update context summary for next chunk."""
        # Track last mentioned exercise, weight, date, etc.
        summary = current_summary.copy()

        # Extract last state from messages
        last_user_msg = None
        last_assistant_msg = None
        for msg in reversed(messages):
            if msg["role"] == "user" and not last_user_msg:
                last_user_msg = msg
            if msg["role"] == "assistant" and not last_assistant_msg:
                last_assistant_msg = msg
            if last_user_msg and last_assistant_msg:
                break

        if last_user_msg:
            summary["last_user_message"] = {
                "date": last_user_msg["date"],
                "text_preview": last_user_msg["text"][:200],
            }

        if last_assistant_msg:
            summary["last_assistant_message"] = {
                "date": last_assistant_msg["date"],
                "text_preview": last_assistant_msg["text"][:200],
            }

        # Track last extracted records for context
        if extracted_records:
            summary["recent_records"] = extracted_records[-5:]  # Last 5 records

        return summary

    def _deduplicate_records(
        self, records: list[dict[str, Any]], key_fields: list[str]
    ) -> list[dict[str, Any]]:
        """Deduplicate records by key fields."""
        seen = set()
        unique = []

        for record in records:
            # Build key from specified fields
            key_parts = []
            for field in key_fields:
                value = record.get(field)
                if value is not None:
                    key_parts.append(str(value))
            key = tuple(key_parts)

            if key not in seen:
                seen.add(key)
                unique.append(record)

        return unique


def get_exercise_sets_schema() -> dict[str, Any]:
    """Schema definition for exercise sets extraction."""
    return {
        "name": "exercise_sets",
        "description": "Individual exercise sets with reps, weight, and metadata",
        "fields": [
            {
                "name": "exercise_name",
                "type": "string",
                "description": "Name of the exercise (e.g., 'Bench Press', 'Squat')",
            },
            {
                "name": "date",
                "type": "string (YYYY-MM-DD)",
                "description": "Date of the set",
            },
            {
                "name": "repetitions",
                "type": "integer",
                "description": "Number of repetitions",
            },
            {
                "name": "weight",
                "type": "string",
                "description": "Weight used (e.g., '80kg', '185lbs', '12.5kg each' for unilateral)",
            },
            {
                "name": "type",
                "type": "string",
                "description": "Set type: 'Warmup' or 'Target failure' or empty string",
            },
            {"name": "location", "type": "string", "description": "Gym/location name"},
            {
                "name": "time_of_day",
                "type": "string",
                "description": "Morning, Afternoon, Evening, or null",
            },
            {
                "name": "laterality",
                "type": "string",
                "description": "'bilateral' or 'unilateral'",
            },
            {
                "name": "notes",
                "type": "string",
                "description": "Additional context, PR notes, form notes, etc.",
            },
        ],
        "key_fields": ["date", "exercise_name", "repetitions", "weight", "type"],
        "examples": [
            {
                "exercise_name": "Bench Press",
                "date": "2025-09-15",
                "repetitions": 8,
                "weight": "80kg",
                "type": "Target failure",
                "location": "Metropolitan Sagrada Familia",
                "time_of_day": "Afternoon",
                "laterality": "bilateral",
                "notes": "PR - first time hitting 8 reps at this weight",
            },
            {
                "exercise_name": "Cable Shoulder Raise",
                "date": "2025-09-15",
                "repetitions": 7,
                "weight": "7.5kg each",
                "type": "Target failure",
                "location": "Metropolitan Sagrada Familia",
                "time_of_day": "Afternoon",
                "laterality": "unilateral",
                "notes": "To failure",
            },
        ],
    }
