# Conversation Parser - Topic-Agnostic Data Extraction

**Purpose:** General-purpose framework for extracting structured data from conversational exports (ChatGPT, etc.) using LLM inference.

**Key Features:**
- Schema-driven extraction (works for any data type)
- Analyzes user + assistant messages together for full context
- Chunked processing for long conversations
- Handles implicit references ("Same", "One more", etc.)
- Topic-agnostic - define a schema for any conversation type

---

## Usage

### Basic Example: Exercise Sets

```python
from conversation_parser import ConversationParser, get_exercise_sets_schema

parser = ConversationParser()
messages = parser.load_conversation(Path("conversation.json"))
schema = get_exercise_sets_schema()

records = parser.extract_with_schema(messages, schema, conversation_title="Track lifting progression")
```

### Command Line: Import Script

```bash
# Pattern matching (fast, no API calls)
python scripts/import_chatgpt_lifting_sets.py --yes

# Inference-based (slower, requires OPENAI_API_KEY, more comprehensive)
python scripts/import_chatgpt_lifting_sets.py --use-inference --yes
```

---

## Creating Custom Schemas

To extract different data types from other conversations, define a schema:

```python
my_schema = {
    "name": "expense_records",  # Schema identifier
    "description": "Financial expenses mentioned in conversation",
    "fields": [
        {
            "name": "amount",
            "type": "float",
            "description": "Expense amount in dollars"
        },
        {
            "name": "category",
            "type": "string",
            "description": "Expense category"
        },
        {
            "name": "date",
            "type": "string (YYYY-MM-DD)",
            "description": "Date of expense"
        },
        # ... more fields
    ],
    "key_fields": ["date", "amount", "category"],  # For deduplication
    "examples": [
        {
            "amount": 45.50,
            "category": "Groceries",
            "date": "2025-01-15"
        }
    ]
}
```

Then use it:

```python
parser = ConversationParser()
messages = parser.load_conversation(Path("expense_chat.json"))
records = parser.extract_with_schema(messages, my_schema)
```

---

## Configuration

### Environment Variables

- `OPENAI_API_KEY`: Required for inference mode

### Parser Options

```python
parser = ConversationParser(
    api_key="sk-...",  # Optional, defaults to OPENAI_API_KEY env var
    model="gpt-4o-mini",  # Model to use
    chunk_size=50,  # Messages per chunk
    overlap=10,  # Messages of overlap between chunks
)
```

---

## How It Works

1. **Load Conversation:** Parses ChatGPT export JSON into chronological message list
2. **Chunk Messages:** Splits long conversations into overlapping chunks (preserves context)
3. **LLM Extraction:** For each chunk:
   - Builds prompt with schema, context from previous chunks, and current messages
   - Calls LLM to extract structured records
   - Updates context summary for next chunk
4. **Deduplication:** Removes duplicates based on key fields
5. **Returns:** List of structured records matching schema

---

## Advantages Over Pattern Matching

- **Context Understanding:** Resolves "Same 8 reps" by tracking previous exercise/weight
- **User Messages:** Captures sets mentioned by user that weren't formatted into tables
- **Flexible Formats:** Handles variations in how data is expressed
- **Topic Agnostic:** Works for any conversation type with appropriate schema
- **Implicit Data:** Extracts implied information from conversational flow

---

## Limitations

- **API Costs:** Requires OpenAI API calls (uses gpt-4o-mini by default for cost efficiency)
- **Slower:** Inference is slower than pattern matching
- **Requires API Key:** Must have OPENAI_API_KEY set
- **Token Limits:** Very long conversations may need larger chunk_size or model with higher context window

---

## Example: Extracting Different Data Types

### Tasks/To-Dos

```python
task_schema = {
    "name": "tasks",
    "description": "Tasks and to-dos mentioned in conversation",
    "fields": [
        {"name": "task", "type": "string", "description": "Task description"},
        {"name": "due_date", "type": "string", "description": "Due date if mentioned"},
        {"name": "priority", "type": "string", "description": "Priority level"},
    ],
    "key_fields": ["task", "due_date"],
}
```

### Contacts

```python
contact_schema = {
    "name": "contacts",
    "description": "Contact information mentioned in conversation",
    "fields": [
        {"name": "name", "type": "string", "description": "Contact name"},
        {"name": "email", "type": "string", "description": "Email address"},
        {"name": "phone", "type": "string", "description": "Phone number"},
    ],
    "key_fields": ["name", "email"],
}
```

---

## Integration with Import Scripts

The conversation parser is designed to be integrated into domain-specific import scripts:

1. Define schema for your data type
2. Load conversation
3. Extract records
4. Transform to match your normalized data schema
5. Write to parquet

See `scripts/import_chatgpt_lifting_sets.py` for a complete example.



