---
name: language
description: "Process language prompts for fluency verification and translation across English, Spanish, and Catalan with Barcelona-appropriate usage."
triggers:
  - language
  - /language
  - fluency check
  - verify fluency
  - translate to spanish and catalan
user_invocable: true
entity_id: ent_88ea22b2cdcc0ff52b247d9c
---

# language

## Purpose

Define how `/language` requests process user text for fluency verification and translation (English, Spanish, Catalan).

## Scope

Applies when the user invokes `/language` in this repository. Covers trigger logic, fluency checks, and translation steps.

## Trigger logic

If the prompt starts with "Question: " (or equivalent in other languages), treat it as a question to answer directly. Do not treat as raw text for fluency/translation.

Equivalent question prefixes:
- English: "Question: ", "Query: "
- Spanish: "Pregunta: ", "Consulta: "
- Catalan: "Pregunta: ", "Consulta: "

Otherwise, treat the remaining text as raw text for fluency verification and translation.

## Processing steps (for raw text)

### 1) Fluency verification

- Determine source language (English, Spanish, or Catalan)
- Verify fluency and naturalness in that language
- If corrections are needed, provide corrected text plus brief explanation for each change
- If already fluent, confirm and optionally suggest refinements

### 2) Translate to other languages

- Translate corrected text into the other two supported languages
- Supported languages: English, Spanish, Catalan
- Adapt wording for Barcelona, Spain (register, vocabulary, regional conventions)

### 3) Output format

For each translation include:

a. Translation text  
The translated phrase/sentence.

b. Phonetics  
- Provide IPA/simple phonetic transcription below each non-English translation
- For Catalan: include phonetics
- For Spanish: omit phonetics

c. Phonetics symbol guide  
Include a phonetics reference link such as:
`[IPA chart](https://www.internationalphoneticassociation.org/content/ipa-chart)`

d. Google Translate link  
Add `GT` link per translation, for example:
`[GT](https://translate.google.com/?sl=en&tl=es&text=ENCODED_TEXT)`

e. Regional relevance (mandatory)  
Include a short note with:
- Primary region (for example Barcelona, Spain; Peninsular Spanish; Central Catalan)
- Register/context (formal, informal, neutral, literary, colloquial)
- Where phrase is more/less common (for example common in Spain, more typical in Latin America)

Place regional relevance after translation blocks and before etymological context.

### 4) Etymological context

After translations, include:
- Etymology and usage notes for key words
- Focus on useful or interesting origins

### 5) Vocabulary suggestions

- Suggest 2-4 alternative words/phrases not used by user
- Aim to expand vocabulary naturally for Barcelona usage

### 6) Fun etymological fact

- Add one brief interesting etymology fact about a word from the phrase

### 7) General communication

- Use English for all explanations and instructions
- Use target languages only for translated text itself

## Constraints

- Never recommend leading inverted punctuation in Spanish as a correction suggestion (do not suggest forms like "¿Hola?"; prefer "Hola?" or standard forms without leading inverted punctuation in these workflow responses)
- Keep translations appropriate for Barcelona, Spain
- Keep translation blocks clearly separated from explanatory text
- Always include regional relevance (region, register, and usage-commonness note)

## Example invocation

User: `/language I want to book a table for two tonight`

Expected handling: verify fluency, translate to Spanish and Catalan, include Catalan phonetics (not Spanish phonetics), include regional relevance, etymology, vocabulary suggestions, and a fun fact.
