"""LLM-based notice parser using Claude Haiku as fallback for regex failures.

When the regex parser in notice_parser.py fails to extract address or owner_name,
this module sends the raw notice text to Claude Haiku for structured extraction.
"""

import logging

import llm_client

logger = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 512

SYSTEM_PROMPT = (
    "You extract structured data from Alabama legal notices. "
    "Return ONLY valid JSON with no markdown formatting, no code fences, no explanation."
)

USER_PROMPT_TEMPLATE = """\
Extract the following fields from this {notice_type} legal notice published in {county} County, Alabama.

Return ONLY a JSON object with these exact keys:
- "address": the property street address (e.g. "123 Main St"). NOT the courthouse, auction location, or trustee office address.
- "city": the city where the property is located
- "state": always "AL"
- "zip": the 5-digit zip code of the property
- "owner_name": the property owner, borrower, or grantor name(s). For foreclosures this is who executed the deed of trust. Use ALL CAPS as written in the notice.
- "auction_date": the scheduled sale/auction date in YYYY-MM-DD format. If the notice contains a postponement chain ("postponed from X until Y"), use the LAST/most recent postponement date. Otherwise use the originally scheduled sale date. NOT the publication date.
- "mortgage_company": the current Mortgagee/Transferee — the entity that now holds the loan and is foreclosing (e.g. "Nationstar Mortgage LLC", "U.S. Bank N.A. as Trustee for ..."). Often appears as "the undersigned [X], as Mortgagee/Transferee". Empty string if not foreclosure.
- "original_lender": the original lender at loan origination (often "Mortgage Electronic Registration Systems, Inc. as nominee for [X]"). Found in "originally in favor of ..." phrasing. Empty string if not foreclosure.
- "trustee": the law firm or substitute trustee conducting the sale (e.g. "Tiffany & Bosco, P.A.", "Sirote & Permutt, P.C."). Usually appears near the end of the notice with their office address. Empty string if not foreclosure.
- "trustee_file_number": the trustee's internal file/reference number (e.g. "25-40447-WF-AL", "TB File Number: ..."). Empty string if not present.

If a field cannot be determined from the text, use an empty string "".

Notice text:
{raw_text}"""

PROBATE_PROMPT_TEMPLATE = """\
Extract the following fields from this probate "Notice to Creditors" published in {county} County, Alabama.

Return ONLY a JSON object with these exact keys:
- "decedent_name": the deceased person's full name (from "Estate of [NAME]"). Use ALL CAPS as written.
- "owner_name": the Personal Representative, Executor, or Administrator name. This is the person appointed to manage the estate. Use ALL CAPS as written. Do NOT include their title (e.g. drop "Administratrix", "Co-Administrator", "Executor").

For the mailing address fields below, follow this priority order:
  1. If the notice gives the PR's OWN mailing address (their home or PO box), use it.
  2. OTHERWISE, if the notice gives an attorney's address (often "Attorney for the
     Executor" or "Attorney for Personal Representative"), use the ATTORNEY's address.
     The attorney is the legal designee for service in the probate proceeding —
     creditors mail there, and so do we. This is the operative deliverable address
     for AL Jefferson/Madison probates which rarely include the PR's home address.
  3. If neither is given, leave these empty.
- "owner_street": street address per priority above (e.g. "2004 Shangri-La Drive" or "PO Box 489")
- "owner_city": city per priority above
- "owner_state": state per priority above (usually "AL")
- "owner_zip": 5-digit zip per priority above
- "owner_address_source": "pr" if you used the PR's address, "attorney" if you used the attorney's. Empty if neither.

- "attorney_name": the attorney's name from the notice signature block, if present (e.g. "Allen C. Jones", "Jacob J. Key, Esq."). Drop "Esq." but keep middle initials. Empty if no attorney named.
- "case_number": the probate case number (e.g. "PC2025-234", "PR-2026-000557", "2026-00053"). Found near "Case No.", "CASE NO:", "CASE NUMBER", or "Case#".
- "judge_name": the Judge of Probate's full name (e.g. "Tammy Brown", "James P. Naftel"). Found near "Honorable", "Hon.", or "Judge of Probate". Drop the title.
- "granted_date": the date Letters Testamentary or Letters of Administration were granted, in YYYY-MM-DD format. Found near "having been granted ... on the X day of MONTH, YYYY" or "granted ... on MONTH X, YYYY". This is NOT the publication date.
- "address": leave as empty string "" (probate notices do not contain the decedent's property address)
- "city": leave as empty string ""
- "state": "AL"
- "zip": leave as empty string ""

If a field cannot be determined from the text, use an empty string "".

Notice text:
{raw_text}"""

EVICTION_PROMPT_TEMPLATE = """\
Extract the following fields from this eviction notice / detainer warrant from {county} County, Alabama.

The PLAINTIFF is the landlord (property owner) — this is who we want to contact.
The DEFENDANT is the tenant being evicted.

Return ONLY a JSON object with these exact keys:
- "owner_name": the PLAINTIFF name (landlord/property owner). Use ALL CAPS as written.
- "address": the rental property street address where the eviction is occurring
- "city": the city where the property is located
- "state": always "AL"
- "zip": the 5-digit zip code of the property
- "case_number": the court case number
- "filing_date": the filing date in YYYY-MM-DD format
- "amount_owed": the amount owed (if stated), as a string like "1500.00"

If a field cannot be determined from the text, use an empty string "".

Notice text:
{raw_text}"""

CODE_VIOLATION_PROMPT_TEMPLATE = """\
Extract the following fields from this code violation notice from {county} County, Alabama.

Return ONLY a JSON object with these exact keys:
- "owner_name": the property owner name. Use ALL CAPS as written.
- "address": the property street address where the violation exists
- "city": the city where the property is located
- "state": always "AL"
- "zip": the 5-digit zip code of the property
- "parcel_id": the parcel ID / tax map number if shown
- "violation_type": brief description of the violation (e.g. "overgrown lot", "condemned structure")
- "compliance_deadline": the compliance deadline date in YYYY-MM-DD format

If a field cannot be determined from the text, use an empty string "".

Notice text:
{raw_text}"""

DIVORCE_PROMPT_TEMPLATE = """\
Extract the following fields from this divorce filing / complaint from {county} County, Alabama.

Return ONLY a JSON object with these exact keys:
- "owner_name": the PETITIONER name (person filing for divorce). Use ALL CAPS as written.
- "spouse_name": the RESPONDENT name (other party). Use ALL CAPS as written.
- "address": the marital home / property address if listed (may be on property schedule page)
- "city": the city where the property is located
- "state": always "AL"
- "zip": the 5-digit zip code of the property
- "case_number": the court case number

If a field cannot be determined from the text, use an empty string "".

Notice text:
{raw_text}"""

AUTO_DETECT_PROMPT_TEMPLATE = """\
Classify this legal document from {county} County, Tennessee into one of these categories:
- "foreclosure" — trustee sale, deed of trust, notice of default
- "tax_sale" — delinquent property tax auction
- "tax_delinquent" — unpaid property taxes, no auction yet
- "probate" — estate of deceased, notice to creditors
- "eviction" — detainer warrant, unlawful detainer, landlord-tenant
- "code_violation" — municipal code enforcement, building violation, condemnation
- "divorce" — dissolution of marriage, divorce complaint, property division

Return ONLY a JSON object with these exact keys:
- "notice_type": one of the categories above
- "confidence": "high", "medium", or "low"

Document text:
{raw_text}"""

# Keys expected from each prompt type
_FORECLOSURE_KEYS = {
    "address", "city", "state", "zip", "owner_name", "auction_date",
    "mortgage_company", "original_lender", "trustee", "trustee_file_number",
}
_PROBATE_KEYS = {
    "decedent_name", "owner_name", "owner_street", "owner_city",
    "owner_state", "owner_zip", "address", "city", "state", "zip",
    "case_number", "judge_name", "granted_date",
    # Note: "owner_address_source" and "attorney_name" are optional
    # bonus fields populated by the prompt's attorney-fallback logic.
    # NOT in expected because the LLM may omit them on notices that
    # lack an attorney signature block — making them required would
    # cause the whole extract to be rejected.
}
_EVICTION_KEYS = {
    "owner_name", "address", "city", "state", "zip",
    "case_number", "filing_date", "amount_owed",
}
_CODE_VIOLATION_KEYS = {
    "owner_name", "address", "city", "state", "zip",
    "parcel_id", "violation_type", "compliance_deadline",
}
_DIVORCE_KEYS = {
    "owner_name", "spouse_name", "address", "city", "state", "zip",
    "case_number",
}
_AUTO_DETECT_KEYS = {"notice_type", "confidence"}


async def extract_with_llm(
    raw_text: str,
    notice_type: str,
    county: str,
    api_key: str,
) -> dict:
    """Call Claude Haiku to extract structured fields from notice text.

    Returns dict with keys: address, city, state, zip, owner_name (+ probate fields).
    Returns empty dict on any failure.
    """
    if not raw_text.strip():
        return {}
    # For Ollama backend, api_key not required
    import config as _cfg
    if getattr(_cfg, "LLM_BACKEND", "anthropic") == "anthropic" and not api_key:
        return {}

    # Truncate to ~8000 chars to stay within token limits while keeping cost low
    text = raw_text[:8000]

    # Route to type-specific prompt
    prompt_map = {
        "probate": (PROBATE_PROMPT_TEMPLATE, _PROBATE_KEYS),
        "eviction": (EVICTION_PROMPT_TEMPLATE, _EVICTION_KEYS),
        "code_violation": (CODE_VIOLATION_PROMPT_TEMPLATE, _CODE_VIOLATION_KEYS),
        "divorce": (DIVORCE_PROMPT_TEMPLATE, _DIVORCE_KEYS),
    }

    if notice_type in prompt_map:
        template, expected = prompt_map[notice_type]
        prompt = template.format(county=county, raw_text=text)
    else:
        # Default: foreclosure / tax_sale / tax_delinquent
        prompt = USER_PROMPT_TEMPLATE.format(
            notice_type=notice_type, county=county, raw_text=text,
        )
        expected = _FORECLOSURE_KEYS

    try:
        parsed = await llm_client.chat_json_async(
            prompt, system=SYSTEM_PROMPT, max_tokens=MAX_TOKENS, api_key=api_key,
        )

        if not parsed:
            return {}

        # Validate expected keys exist
        if not expected.issubset(parsed.keys()):
            logger.warning("LLM response missing expected keys: %s", parsed.keys())
            return {}

        # Clean up values
        for key in expected:
            if not isinstance(parsed[key], str):
                parsed[key] = str(parsed[key]) if parsed[key] else ""
            parsed[key] = parsed[key].strip()

        if notice_type == "probate":
            logger.info(
                "LLM extracted: decedent='%s', pr='%s', pr_addr='%s'",
                parsed.get("decedent_name", ""),
                parsed.get("owner_name", ""),
                parsed.get("owner_street", ""),
            )
        else:
            logger.info(
                "LLM extracted: address='%s', owner='%s'",
                parsed.get("address", ""),
                parsed.get("owner_name", ""),
            )
        return parsed

    except Exception as e:
        logger.warning("LLM extraction failed: %s", e)
        return {}


_COUNTY_CLASSIFY_PROMPT = """\
This is a legal notice published in Alabama (foreclosure, probate,
tax-sale, code-violation, etc.). Identify the Alabama county where this
matter is being filed/recorded.

Look for the strongest signal in priority order:
  1. The Probate Court/Judge of Probate's county ("Judge of Probate of X County")
  2. The "STATE OF ALABAMA / COUNTY OF X" pleading header
  3. Recorded in / Office of the Judge of Probate of X County
  4. The X County Courthouse or "X County, Alabama" mention
  5. Property situs address city → infer the county

Return ONLY JSON in this format:
{{"county": "<lowercase county name>", "confidence": "high"|"medium"|"low"}}

Examples of valid responses:
  {{"county": "jefferson", "confidence": "high"}}
  {{"county": "limestone", "confidence": "medium"}}
  {{"county": "unknown", "confidence": "low"}}

If the notice mentions multiple counties (e.g. trustee's office in one,
property in another), pick the county where the MATTER itself is being
prosecuted/recorded — usually that's the probate court for probate notices,
the deed-recording office for foreclosure.

Notice text:
{raw_text}"""


async def extract_county_from_notice(raw_text: str, api_key: str) -> str:
    """Classify the AL county where a notice is being filed/recorded.

    Used as a tiebreaker when regex-based county detection in
    notice_parser.is_target_county() returns ambiguous results (zero
    matches or multiple counties). Returns the lowercase county name
    (e.g. "jefferson", "limestone") or "" if classification fails or
    confidence is low.
    """
    if not raw_text.strip():
        return ""
    import config as _cfg
    if getattr(_cfg, "LLM_BACKEND", "anthropic") == "anthropic" and not api_key:
        return ""

    text = raw_text[:6000]
    prompt = _COUNTY_CLASSIFY_PROMPT.format(raw_text=text)

    try:
        parsed = await llm_client.chat_json_async(
            prompt, system=SYSTEM_PROMPT, max_tokens=64, api_key=api_key,
        )
        if not parsed:
            return ""
        county = (parsed.get("county") or "").strip().lower()
        confidence = (parsed.get("confidence") or "low").strip().lower()
        if not county or county == "unknown":
            return ""
        if confidence == "low":
            return ""
        return county
    except Exception as e:
        logger.warning("County LLM classification failed: %s", e)
        return ""


async def auto_detect_notice_type(
    raw_text: str,
    county: str,
    api_key: str,
) -> str | None:
    """Use LLM to classify notice type from OCR text.

    Returns notice_type string or None if classification fails.
    Used as fallback when folder path doesn't indicate notice type.
    """
    if not raw_text.strip():
        return None

    import config as _cfg
    if getattr(_cfg, "LLM_BACKEND", "anthropic") == "anthropic" and not api_key:
        return None

    text = raw_text[:4000]  # Less text needed for classification
    prompt = AUTO_DETECT_PROMPT_TEMPLATE.format(county=county, raw_text=text)

    try:
        parsed = await llm_client.chat_json_async(
            prompt, system=SYSTEM_PROMPT, max_tokens=64, api_key=api_key,
        )
        if not parsed or "notice_type" not in parsed:
            return None

        detected = parsed["notice_type"].strip().lower()
        confidence = parsed.get("confidence", "low").strip().lower()

        valid_types = {
            "foreclosure", "tax_sale", "tax_delinquent", "probate",
            "eviction", "code_violation", "divorce",
        }
        if detected not in valid_types:
            logger.warning("LLM detected unknown notice type: %s", detected)
            return None

        logger.info("Auto-detected notice type: %s (confidence: %s)", detected, confidence)
        return detected

    except Exception as e:
        logger.warning("Notice type auto-detection failed: %s", e)
        return None
