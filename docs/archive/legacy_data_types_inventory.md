# Data Types

60+ normalized data types with 35,000+ total records across finance, admin, work, health, and other domains.

## Finance (25 types)

- **transactions** (4,007) - Bank and card transactions
- **holdings** (195) - Portfolio positions and asset holdings
- **balances** (20) - Account balance snapshots
- **income** (94) - Income stream data
- **tax_events** (49) - Capital gains, losses, and tax events
- **tax_filings** (25) - Tax filing tracking
- **crypto_transactions** (351) - On-chain cryptocurrency transactions
- **flows** (29) - Cash flow entries
- **fixed_costs** (117) - Recurring expenses and subscriptions
- **liabilities** (7) - Debt and liability tracking
- **investments** (2) - Investment holdings categories
- **transfers** (3) - Asset transfers between accounts
- **wallets** (49) - Wallets and financial institutions
- **asset_types** (6) - Asset type definitions
- **asset_values** (3) - Historical asset value snapshots
- **equity_units** (2) - Stock options, RSUs, and equity grants
- **bank_certificates** (4) - Bank certificate tracking
- **financial_strategies** (1) - Financial strategy decisions
- **accounts** (46) - Financial accounts within wallets/institutions
- **account_identifiers** (8,670) - Account identifiers and numbers
- **companies** (574) - Company and business entity tracking
- **contracts** (2) - Legal contracts and agreements
- **orders** (1) - Trading orders
- **properties** (2) - Real estate property tracking
- **property_equipment** (1) - Property equipment tracking

## Admin (11 types)

- **contacts** (313) - Contact and relationship tracking
- **addresses** (19) - Physical addresses
- **user_accounts** (1,344) - Online service accounts and credentials
- **events** (5) - One-time events and trips
- **recurring_events** (64) - Recurring scheduled events
- **locations** (38) - Geographic locations
- **purchases** (9) - Purchase tracking
- **people** (134) - Personal relationships
- **goals** (38) - Personal and professional goals
- **projects** (9) - Project tracking
- **transcriptions** (1) - Audio transcription records

## Work (8 types)

- **tasks** (15,358) - Task management and tracking
- **task_comments** (2) - Task comments
- **task_attachments** (3) - Task file attachments
- **task_stories** (516) - Task history and stories
- **task_custom_fields** - Custom field definitions
- **task_dependencies** - Task dependency relationships
- **priorities** - Priority definitions
- **problems** - Problem tracking
- **responsibilities** - Responsibility assignments
- **beliefs** (15) - Belief and principle tracking
- **arguments** (14) - Argument and reasoning tracking

## Health (6 types)

- **workouts** (10) - Workout routines
- **exercises** (99) - Exercise definitions
- **sets** (6,601) - Individual exercise sets with reps and weight
- **meals** (6) - Meal tracking with nutritional information
- **foods** (34) - Food database
- **health_reports** - Health reports and assessments

## Habits (3 types)

- **habits** (82) - Habit tracking for recurring behaviors and daily practices
- **habit_completions** - Daily completion tracking for habits
- **habit_objectives** (110) - Target benefits and objectives for habits; links to habits via `habit_id`

## Content (1 type)

- **content_sources** (2) - External content sources (newsletters, RSS feeds, blogs) for content generation context and industry intelligence

## Workspace / Analysis (1 type)

- **repositories** - Sibling repositories (parent directory `../`). Used by `/analyze` for comparative analysis against all repos. Synced via `execution/scripts/sync_repos_to_parquet.py`.

## Other

- **emotions** (15) - Emotion tracking
- **movies** (1) - Movie tracking

## Schema Validation

All data types follow schema-driven validation with:
- **Automatic snapshots** before all data modifications (stored in `$DATA_DIR/snapshots/`)
- **Audit logging** tracking all changes with rollback capabilities
- **Type safety** enforced through JSON schemas
- **Schema evolution** support for adding new fields and types

See `$DATA_DIR/schemas/` for complete schema definitions for all data types.

