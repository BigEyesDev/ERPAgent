# Email-to-ERP Evaluation Dataset

This directory contains deterministic, fully labelled fixtures for testing email
ingestion, document parsing, structured extraction, ERP validation, duplicate
detection, security gates, human review, and mocked order-creation decisions.
Every company, person, address, message, and order is fictional.

## Layout

- `emails/`: genuine MIME messages generated with Python's `EmailMessage`
- `attachments/`: PDF, PPTX, XLSX, and controlled XLSM test documents
- `erp/`: fictional customer, product, price, history, and order master data
- `expected/`: extraction ground truth, provenance, outcomes, and reason codes
- `manifests/fixture_manifest.json`: machine-readable fixture index

Regenerate from the repository root:

```bash
python scripts/generate_fixtures.py
pytest tests/test_generated_fixtures.py -v
```

The command is safe to repeat. It replaces generated files in these dataset
directories and emits functionally identical content. Use `--output-root` for
an isolated copy.

## Fixture Catalogue

| ID | Fixture | Attachment | Main condition | Expected outcome |
| -- | ------- | ---------- | -------------- | ---------------- |
| 001 | valid_plain_text | None | Complete plain-text order | AUTO_CREATE |
| 002 | valid_pdf | order_valid.pdf | Email/PDF merge | AUTO_CREATE |
| 003 | valid_pptx | order_valid.pptx | Cross-slide extraction | AUTO_CREATE |
| 004 | valid_xlsx | order_valid.xlsx | Typed spreadsheet values | AUTO_CREATE |
| 005 | german_order | order_german.pdf | German locale and large quantity | HUMAN_REVIEW |
| 006 | missing_quantity | order_missing_quantity.xlsx | Missing quantity | CLARIFICATION_REQUIRED |
| 007 | ambiguous_usual_order | None | Historical-order ambiguity | HUMAN_REVIEW |
| 008 | unknown_customer | None | Unknown customer | HUMAN_REVIEW |
| 009 | unknown_product | order_unknown_product.xlsx | Unknown product | CLARIFICATION_REQUIRED |
| 010 | price_mismatch | order_price_mismatch.pdf | Price mismatch | HUMAN_REVIEW |
| 011 | large_quantity | order_large_quantity.xlsx | Quantity/value thresholds | HUMAN_REVIEW |
| 012 | update_request | order_update_request.pdf | Order update | HUMAN_REVIEW |
| 013 | cancel_request | order_cancel_request.pdf | Order cancellation | HUMAN_REVIEW |
| 014 | email_attachment_conflict | order_conflict.xlsx | Cross-source quantity conflict | HUMAN_REVIEW |
| 015 | prompt_injection_email | None | Email prompt injection | SECURITY_QUARANTINE |
| 016 | prompt_injection_attachment | order_prompt_injection.pdf | Attachment prompt injection | SECURITY_QUARANTINE |
| 017 | encrypted_pdf | order_encrypted.pdf | Encrypted PDF | SECURITY_QUARANTINE |
| 018 | malformed_pdf | order_malformed.pdf | Malformed PDF | CLARIFICATION_REQUIRED |
| 019 | macro_enabled_workbook | Unavailable* | Macro-enabled workbook | SECURITY_QUARANTINE |
| 020 | extension_mismatch | order_extension_mismatch.xlsm | Extension/content mismatch | SECURITY_QUARANTINE |
| 021 | duplicate_original | None | Original order | AUTO_CREATE |
| 022 | duplicate_replay | None | Duplicate replay | DUPLICATE_NOOP |
| 023 | scanned_pdf | order_scanned.pdf | Image-only scan | HUMAN_REVIEW |
| 024 | multisheet_xlsx | order_multisheet.xlsx | Multi-sheet workbook | AUTO_CREATE |
| 025 | conflicting_customer_identity | order_customer_conflict.pdf | Customer identity conflict | HUMAN_REVIEW |
| 026 | malformed_business_values | order_invalid_values.xlsx | Malformed business values | CLARIFICATION_REQUIRED |

## Known Limitations and Safety

The scanned PDF is deliberately image-only. OCR is not run by the generator or
the demo, so fixture 023 routes to human review.

Fixture 019 is generated only when a trusted, benign `.xlsm` template containing
`xl/vbaProject.bin` is supplied with `--macro-template`. Its current availability
is **not generated**. This constraint is
intentional: `openpyxl` cannot safely create a VBA project, and the generator
will not download, fabricate, inspect macro behavior, or execute active content.

The encrypted PDF uses the deterministic test-only password `TestOnly-2026!`.
The password is not included in its email and workflow code must request an
unencrypted copy rather than decrypting it automatically. Encrypted attachment
bytes may differ across runs because secure encryption uses fresh randomness.

Prompt-injection text is inert natural-language evaluation content. The dataset
contains no real credentials, executable payloads, external formulas, or harmful
macros. No active content, formulas, attachments, or VBA should ever be executed.

`TECHNICAL_FAILURE` is part of the workflow outcome vocabulary but is not assigned
to malformed customer input: fixture 018 intentionally uses
`CLARIFICATION_REQUIRED`. Infrastructure-failure injection can be layered onto
these deterministic fixtures by a workflow test harness.
