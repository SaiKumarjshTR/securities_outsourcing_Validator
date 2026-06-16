"""
core/fix_templates.py
──────────────────────
Fix template library for common Carswell SGML keying errors.

Each template provides:
  - problem   : plain-English description of the rule
  - wrong     : example of the incorrect markup
  - correct   : example of the correct markup
  - steps     : numbered step-by-step instructions for the keyer
  - time_est  : estimated time to fix

Usage:
    from validator.core.fix_templates import get_fix_template, enrich_issue

    # Enrich a single issue dict in-place:
    enrich_issue(issue)

    # Or look up directly:
    tmpl = get_fix_template("POLIDOC_MISSING_LANG")
    if tmpl:
        print(tmpl["steps"])
"""

from typing import Optional

# ── Template registry ─────────────────────────────────────────────────────────
# Key convention: CATEGORY_DESCRIPTION  (upper-snake-case)
# Mapped from (category, keyword-in-description) pairs in enrich_issue().

_TEMPLATES: dict[str, dict] = {

    # ── DTD / Schema ──────────────────────────────────────────────────────────

    "POLIDOC_MISSING_LANG": {
        "problem": "Every Carswell document requires a LANG attribute on <POLIDOC> "
                   "specifying the language (EN or FR).",
        "wrong":   '<POLIDOC LABEL="NI 31-103" ADDDATE="20260101">',
        "correct": '<POLIDOC LANG="EN" LABEL="NI 31-103" ADDDATE="20260101">',
        "steps":   "1. Open the SGML file and search for '<POLIDOC'.\n"
                   "2. Inside the opening tag, add LANG=\"EN\" (or LANG=\"FR\" if French).\n"
                   "3. Ensure the attribute appears before or after LABEL — order does not matter.\n"
                   "4. Save and re-validate.",
        "time_est": "30 seconds",
    },

    "POLIDOC_MISSING_LABEL": {
        "problem": "Every Carswell document requires a LABEL attribute on <POLIDOC> "
                   "containing the document identifier (e.g. 'NI 31-103').",
        "wrong":   '<POLIDOC LANG="EN" ADDDATE="20260101">',
        "correct": '<POLIDOC LANG="EN" LABEL="NI 31-103" ADDDATE="20260101">',
        "steps":   "1. Search for '<POLIDOC' in the SGML file.\n"
                   "2. Add LABEL=\"<document number>\" inside the tag.\n"
                   "3. Save and re-validate.",
        "time_est": "1 minute",
    },

    "POLIDOC_MISSING_ADDDATE": {
        "problem": "ADDDATE attribute on <POLIDOC> must be present and in YYYYMMDD format.",
        "wrong":   '<POLIDOC LANG="EN" LABEL="NI 31-103">',
        "correct": '<POLIDOC LANG="EN" LABEL="NI 31-103" ADDDATE="20260101">',
        "steps":   "1. Search for '<POLIDOC' in the SGML file.\n"
                   "2. Add ADDDATE=\"YYYYMMDD\" using the document's publication date.\n"
                   "3. Save and re-validate.",
        "time_est": "1 minute",
    },

    "POLIDOC_ADDDATE_FORMAT": {
        "problem": "ADDDATE must be exactly 8 digits in YYYYMMDD format (no slashes or dashes).",
        "wrong":   'ADDDATE="2026-01-15"',
        "correct": 'ADDDATE="20260115"',
        "steps":   "1. Search for 'ADDDATE=' in the SGML file.\n"
                   "2. Remove any dashes or slashes from the date value.\n"
                   "3. Ensure the result is exactly 8 digits: YYYYMMDD.\n"
                   "4. Save and re-validate.",
        "time_est": "30 seconds",
    },

    "POLIDOC_MISSING_ROOT": {
        "problem": "The document has no <POLIDOC> root element. Every Carswell SGML "
                   "document must open with <POLIDOC ...> and close with </POLIDOC>.",
        "wrong":   "<FREEFORM>\n  <BLOCK1>...",
        "correct":  '<POLIDOC LANG="EN" LABEL="..." ADDDATE="YYYYMMDD">\n'
                    "<FREEFORM>\n  <BLOCK1>...",
        "steps":   "1. Add '<POLIDOC LANG=\"EN\" LABEL=\"...\" ADDDATE=\"YYYYMMDD\">' "
                   "as the very first line of the SGML file.\n"
                   "2. Add '</POLIDOC>' as the very last line.\n"
                   "3. Fill in LABEL with the document number and ADDDATE with the date.\n"
                   "4. Save and re-validate.",
        "time_est": "2 minutes",
    },

    # ── BLOCK Nesting ─────────────────────────────────────────────────────────

    "BLOCK_HIERARCHY_INVERSION": {
        "problem": "BLOCK levels must ascend from parent to child. A lower-numbered BLOCK "
                   "(e.g. BLOCK2) should never be nested inside a higher-numbered BLOCK "
                   "(e.g. BLOCK3). This is a hierarchy inversion.",
        "wrong":   "<BLOCK3>\n  <BLOCK2>  ← WRONG: BLOCK2 inside BLOCK3",
        "correct":  "<BLOCK2>\n  <BLOCK3>  ← CORRECT: BLOCK3 inside BLOCK2",
        "steps":   "1. Use the location field to find the inverted BLOCK tag.\n"
                   "2. Examine the structural context: what level should this section be?\n"
                   "3. Either promote the outer BLOCK (decrease its number) or demote "
                   "the inner BLOCK (increase its number) so that child > parent.\n"
                   "4. Ensure all matching closing tags are updated to match.\n"
                   "5. Save and re-validate.",
        "time_est": "5-10 minutes per occurrence",
    },

    "SGMLTBL_OUTSIDE_TABLE": {
        "problem": "<SGMLTBL> must always appear inside a <TABLE> wrapper. "
                   "A standalone <SGMLTBL> without a parent <TABLE> is invalid.",
        "wrong":   "<SGMLTBL>\n  <TBLBODY>...",
        "correct":  "<TABLE>\n<SGMLTBL>\n  <TBLBODY>...</SGMLTBL>\n</TABLE>",
        "steps":   "1. Search for '<SGMLTBL' in the file.\n"
                   "2. Check if it is immediately preceded by '<TABLE'.\n"
                   "3. If not, wrap it: add '<TABLE>' before and '</TABLE>' after "
                   "the entire SGMLTBL block.\n"
                   "4. Save and re-validate.",
        "time_est": "2 minutes per table",
    },

    "P_IN_TBLCELL": {
        "problem": "<P> tags are forbidden inside <TBLCELL>. Table cell content must "
                   "be plain text or inline tags only.",
        "wrong":   "<TBLCELL><P>Cell text here</P></TBLCELL>",
        "correct":  "<TBLCELL>Cell text here</TBLCELL>",
        "steps":   "1. Search for '<P' inside any TBLCELL blocks.\n"
                   "2. Remove the <P> opening and </P> closing tags, keeping the text.\n"
                   "3. If the cell has multiple paragraphs, separate them with a space "
                   "or <NEWLINE/> instead.\n"
                   "4. Save and re-validate.",
        "time_est": "3 minutes per table",
    },

    "TABLE_MISSING_SGMLTBL": {
        "problem": "Every <TABLE> element must contain at least one <SGMLTBL> child. "
                   "A TABLE without SGMLTBL has no actual table data.",
        "wrong":   "<TABLE>\n  <!-- content without SGMLTBL -->\n</TABLE>",
        "correct":  "<TABLE>\n<SGMLTBL>\n  <TBLBODY>\n    <TBLROW>...",
        "steps":   "1. Find the TABLE block flagged in the error.\n"
                   "2. If the table data is present but not wrapped, add the SGMLTBL "
                   "structure around it: <SGMLTBL><TBLBODY><TBLROW><TBLCELL>...</\n"
                   "3. If the table data is genuinely missing, copy it from the source PDF.\n"
                   "4. Save and re-validate.",
        "time_est": "5-15 minutes per table",
    },

    "SGMLTBL_MISSING_TBLBODY": {
        "problem": "Every <SGMLTBL> must contain a <TBLBODY> section wrapping the rows.",
        "wrong":   "<SGMLTBL>\n  <TBLROW>...",
        "correct":  "<SGMLTBL>\n  <TBLBODY>\n    <TBLROW>...\n  </TBLBODY>\n</SGMLTBL>",
        "steps":   "1. Find the SGMLTBL block missing TBLBODY.\n"
                   "2. Wrap all <TBLROW> elements inside a <TBLBODY>...</TBLBODY>.\n"
                   "3. Save and re-validate.",
        "time_est": "2 minutes per table",
    },

    # ── Entity / Character Encoding ───────────────────────────────────────────

    "BARE_LT_CHARACTERS": {
        "problem": "Literal '<' characters in text content must be encoded as '&lt;'. "
                   "A bare '<' that is not opening a tag causes XML parse failure.",
        "wrong":   "<P>For values x < 5, apply the rule.</P>",
        "correct":  "<P>For values x &lt; 5, apply the rule.</P>",
        "steps":   "1. Use Find & Replace in your text editor.\n"
                   "2. Search for: <  (less-than that is NOT immediately followed by "
                   "a capital letter/slash — i.e. not a tag).\n"
                   "3. Replace with: &lt;\n"
                   "   Tip: In most editors, use a regex: <(?![A-Z/]) → &lt;\n"
                   "4. Do NOT replace '<' inside actual SGML tags.\n"
                   "5. Save and re-validate.",
        "time_est": "5 minutes (use Find & Replace)",
    },

    "BARE_AMP_CHARACTERS": {
        "problem": "Literal '&' characters in text content must be encoded as '&amp;'. "
                   "A bare '&' that is not starting an entity reference is invalid.",
        "wrong":   "<P>Smith & Jones Ltd.</P>",
        "correct":  "<P>Smith &amp; Jones Ltd.</P>",
        "steps":   "1. Use Find & Replace in your text editor.\n"
                   "2. Search for: &  (ampersand NOT followed by a word and semicolon).\n"
                   "   Tip regex: &(?![a-zA-Z#][a-zA-Z0-9-]*;)\n"
                   "3. Replace with: &amp;\n"
                   "4. Save and re-validate.",
        "time_est": "5 minutes (use Find & Replace)",
    },

    "UNKNOWN_ENTITY": {
        "problem": "Entity references must use names from the Carswell entity list "
                   "(CARSWELL.ENT). Unknown entity names will be rejected.",
        "wrong":   "&emdash;  ← not in Carswell entity list",
        "correct":  "&mdash;   ← correct Carswell entity name",
        "steps":   "1. Note the unknown entity name from the error description.\n"
                   "2. Look up the correct Carswell entity name in CARSWELL.ENT.\n"
                   "   Common substitutions: &emdash; → &mdash;  |  &endash; → &ndash;\n"
                   "3. Use Find & Replace to correct all occurrences.\n"
                   "4. Save and re-validate.",
        "time_est": "5-10 minutes",
    },

    # ── Content Rules ─────────────────────────────────────────────────────────

    "EMPTY_N_TAG": {
        "problem": "Empty <N></N> tags are forbidden. The <N> tag must contain the "
                   "section/provision number text.",
        "wrong":   "<N></N>\n<TI>Definitions</TI>",
        "correct":  "<N>1.</N>\n<TI>Definitions</TI>",
        "steps":   "1. Use the line number from the error to locate the empty <N> tag.\n"
                   "2. Check the source PDF at the same section to find the number.\n"
                   "3. Insert the number between the tags: <N>1.</N>.\n"
                   "4. If no number exists in the source, remove the <N></N> tags entirely.\n"
                   "5. Save and re-validate.",
        "time_est": "2 minutes per occurrence",
    },

    "EMPTY_TI_TAG": {
        "problem": "Empty <TI></TI> tags (outside EDITNOTE) are forbidden. "
                   "The <TI> tag must contain the section heading text.",
        "wrong":   "<N>1.</N>\n<TI></TI>",
        "correct":  "<N>1.</N>\n<TI>Definitions</TI>",
        "steps":   "1. Use the line number from the error to locate the empty <TI> tag.\n"
                   "2. Check the source PDF to find the heading text for this section.\n"
                   "3. Insert the heading text: <TI>Heading Text Here</TI>.\n"
                   "4. If the heading is genuinely absent in the source, remove the "
                   "<N></N><TI></TI> pair and the parent BLOCK.\n"
                   "5. Save and re-validate.",
        "time_est": "2 minutes per occurrence",
    },

    "TI_ENDS_WITH_PERIOD": {
        "problem": "Section headings (<TI> tags) must not end with a period. "
                   "This is a Carswell keying rule for all document types.",
        "wrong":   "<TI>Definitions and Interpretation.</TI>",
        "correct":  "<TI>Definitions and Interpretation</TI>",
        "steps":   "1. Use Find & Replace with regex: (<TI>[^<]+)\\.</TI>\n"
                   "   Replace with: $1</TI>  (removes trailing period before </TI>).\n"
                   "2. Manually verify each replacement to ensure the period was truly "
                   "a heading-end period (not an abbreviation like 'sec. 2').\n"
                   "3. Save and re-validate.",
        "time_est": "5 minutes (use regex Find & Replace)",
    },

    "URL_NOT_IN_LINE": {
        "problem": "URLs in body text (<P> tags) must be wrapped in a <LINE> tag. "
                   "Bare URLs in <P> are not properly tagged.",
        "wrong":   "<P>See more at https://www.example.com for details.</P>",
        "correct":  "<P>See more at <LINE>https://www.example.com</LINE> for details.</P>",
        "steps":   "1. Find the URL mentioned in the error (check the paragraph text).\n"
                   "2. Wrap the URL in <LINE>...</LINE> tags.\n"
                   "3. If the URL is already in an <EM> tag, that is also acceptable.\n"
                   "4. Save and re-validate.",
        "time_est": "1-2 minutes per URL",
    },

    # ── POLIDENT / Legal Rules ────────────────────────────────────────────────

    "POLIDENT_MISSING": {
        "problem": "The <POLIDENT> element is required in all Carswell documents. "
                   "It contains the document's identifier number and title.",
        "wrong":   "<POLIDOC ...>\n<FREEFORM>  ← POLIDENT missing entirely",
        "correct":  "<POLIDOC ...>\n<POLIDENT>\n  <N>NI 31-103</N>\n"
                    "  <TI>Registration Requirements...</TI>\n</POLIDENT>\n<FREEFORM>",
        "steps":   "1. After the <POLIDOC> opening tag, add a <POLIDENT> block.\n"
                   "2. Inside POLIDENT, add <N>document number</N>.\n"
                   "3. Add <TI>Full document title</TI>.\n"
                   "4. Check the source PDF cover page for the exact number and title.\n"
                   "5. Save and re-validate.",
        "time_est": "5 minutes",
    },

    "POLIDENT_MISSING_N": {
        "problem": "The <POLIDENT> element is missing its <N> (document number) child. "
                   "The <N> tag should contain the official document number.",
        "wrong":   "<POLIDENT>\n  <TI>Registration Requirements</TI>\n</POLIDENT>",
        "correct":  "<POLIDENT>\n  <N>NI 31-103</N>\n  <TI>Registration Requirements</TI>\n</POLIDENT>",
        "steps":   "1. Find <POLIDENT> in the SGML file.\n"
                   "2. Add <N>document number here</N> as the first child.\n"
                   "3. Check the source PDF for the document number.\n"
                   "4. Save and re-validate.",
        "time_est": "2 minutes",
    },

    "POLIDENT_MISSING_TI": {
        "problem": "The <POLIDENT> element is missing its <TI> (document title) child.",
        "wrong":   "<POLIDENT>\n  <N>NI 31-103</N>\n</POLIDENT>",
        "correct":  "<POLIDENT>\n  <N>NI 31-103</N>\n  <TI>Registration Requirements</TI>\n</POLIDENT>",
        "steps":   "1. Find <POLIDENT> in the SGML file.\n"
                   "2. Add <TI>document title here</TI> after the <N> tag.\n"
                   "3. Check the source PDF cover page for the exact title.\n"
                   "4. Save and re-validate.",
        "time_est": "2 minutes",
    },

    "DEF_MISSING_DEFP": {
        "problem": "Every <DEF> element must contain at least one <DEFP> child. "
                   "The DEF → DEFP → TERM hierarchy is required by the Carswell DTD.",
        "wrong":   "<DEF>\n  <TERM>affiliate</TERM>\n  <P>means...</P>\n</DEF>",
        "correct":  "<DEF>\n  <DEFP>\n    <TERM>affiliate</TERM>\n    <P>means...</P>\n"
                    "  </DEFP>\n</DEF>",
        "steps":   "1. Find all <DEF> blocks in the document.\n"
                   "2. For each DEF that lacks DEFP, wrap the TERM and P content "
                   "inside <DEFP>...</DEFP>.\n"
                   "3. Structure: DEF > DEFP > TERM (definition term) + P (definition text).\n"
                   "4. Save and re-validate.",
        "time_est": "3-5 minutes per definition block",
    },

    "GRAPHIC_MISSING_FILENAME": {
        "problem": "Every <GRAPHIC> tag must have a FILENAME attribute pointing to the "
                   "associated image file.",
        "wrong":   "<GRAPHIC>",
        "correct":  '<GRAPHIC FILENAME="SB000001.BMP">',
        "steps":   "1. Find the <GRAPHIC> tag flagged in the error.\n"
                   "2. Add FILENAME=\"SBxxxxxx.BMP\" where xxxxxx is a 6-digit sequence number.\n"
                   "3. Ensure the image file exists in the document package with that name.\n"
                   "4. Save and re-validate.",
        "time_est": "2 minutes per graphic",
    },

    "GRAPHIC_WRONG_FILENAME_FORMAT": {
        "problem": "GRAPHIC FILENAME must follow the format SBxxxxxx.BMP "
                   "(prefix 'SB' + exactly 6 digits + '.BMP' extension).",
        "wrong":   'FILENAME="figure1.jpg"  or  FILENAME="image.bmp"',
        "correct":  'FILENAME="SB000001.BMP"',
        "steps":   "1. Rename the image file to match the SBxxxxxx.BMP format.\n"
                   "2. Update the FILENAME attribute in the SGML to match the new name.\n"
                   "3. Ensure the .BMP extension is uppercase.\n"
                   "4. Save and re-validate.",
        "time_est": "5 minutes per graphic",
    },

    # ── L1 Content Fidelity ───────────────────────────────────────────────────

    "LOW_PARAGRAPH_COVERAGE": {
        "problem": "The SGML is missing a significant number of body paragraphs that "
                   "are present in the source PDF. The 'missing_paragraphs' list in the "
                   "validation report shows the actual missing text.",
        "wrong":   "(paragraph text from PDF not appearing anywhere in SGML)",
        "correct":  "(paragraph keyed inside the appropriate BLOCK/P structure)",
        "steps":   "1. Open the validation report and find 'missing_paragraphs'.\n"
                   "2. For each missing paragraph, search the source PDF to find its location.\n"
                   "3. In the SGML, find the correct structural position (which BLOCK section).\n"
                   "4. Insert the missing paragraph as <P>text</P> (or <P1>/<P2> if a list item).\n"
                   "5. Re-validate after adding a batch of paragraphs.",
        "time_est": "2-5 minutes per missing paragraph",
    },

    "MISSING_HEADINGS": {
        "problem": "Section headings present in the source PDF are not present as <TI> "
                   "tags in the SGML. The 'missing_headings' list shows which ones.",
        "wrong":   "(heading text from PDF has no corresponding <TI> in SGML)",
        "correct":  "<BLOCK1>\n  <N>Part 1</N>\n  <TI>Definitions</TI>\n  ...",
        "steps":   "1. Open the validation report and find 'missing_headings'.\n"
                   "2. For each missing heading, find its position in the source PDF.\n"
                   "3. In the SGML, locate the corresponding section content.\n"
                   "4. Add the missing BLOCK structure with <N> and <TI> tags.\n"
                   "5. Ensure the BLOCK level matches the heading's depth in the document.\n"
                   "6. Re-validate.",
        "time_est": "5-10 minutes per missing heading",
    },

    "CATASTROPHIC_CONTENT_LOSS": {
        "problem": "The SGML contains less than 30% of the word count of the source PDF. "
                   "This indicates that large sections of the document were not keyed.",
        "wrong":   "(SGML with only introduction/summary, missing body sections)",
        "correct":  "(SGML containing all sections present in source PDF)",
        "steps":   "1. Open the source PDF and the SGML side by side.\n"
                   "2. Identify which sections/pages of the PDF are entirely absent from SGML.\n"
                   "3. Key the missing sections, maintaining the correct BLOCK hierarchy.\n"
                   "4. This is a significant rework — prioritise by section importance.\n"
                   "5. Re-validate after completing major sections.",
        "time_est": "Hours — requires full re-keying of missing sections",
    },

    "MISSING_TABLES": {
        "problem": "The source PDF contains tables that are not present in the SGML output. "
                   "Each missing table must be keyed as <TABLE><SGMLTBL>...",
        "wrong":   "(table data from PDF not represented in SGML)",
        "correct":  "<TABLE>\n<SGMLTBL>\n  <TBLBODY>\n    <TBLROW>\n"
                    "      <TBLCELL>Col 1</TBLCELL>\n      <TBLCELL>Col 2</TBLCELL>\n"
                    "    </TBLROW>\n  </TBLBODY>\n</SGMLTBL>\n</TABLE>",
        "steps":   "1. Identify the missing tables from the source PDF.\n"
                   "2. For each table: create TABLE > SGMLTBL > TBLBODY > TBLROW > TBLCELL structure.\n"
                   "3. Place the keyed table in the correct position in the SGML.\n"
                   "4. Add TBLNOTES if the table has footnotes.\n"
                   "5. Re-validate.",
        "time_est": "15-30 minutes per complex table",
    },

    # ── L4 Completeness (D4) ──────────────────────────────────────────────────

    "D4_MISSING_TABLES": {
        "problem": "The source PDF contains tables but the SGML has no <TABLE> tags. "
                   "Tables were likely dropped during ABBYY→DOCX or pipeline conversion. "
                   "Open the PDF, find each table, and key it manually.",
        "wrong":   "(table data from PDF rendered as plain paragraphs or absent entirely)",
        "correct":  "<TABLE>\n<SGMLTBL>\n  <TBLBODY>\n    <TBLROW>\n"
                    "      <TBLCELL>Col 1</TBLCELL>\n      <TBLCELL>Col 2</TBLCELL>\n"
                    "    </TBLROW>\n  </TBLBODY>\n</SGMLTBL>\n</TABLE>",
        "steps":   "1. Open the source PDF and count tables (the validator description shows the number).\n"
                   "2. For each table: find its location in the PDF (page number, section heading).\n"
                   "3. Locate the same position in the SGML (search for surrounding paragraph text).\n"
                   "4. Insert <TABLE><SGMLTBL><TBLBODY><TBLROW><TBLCELL>...</TBLCELL></TBLROW>"
                   "</TBLBODY></SGMLTBL></TABLE> at that position.\n"
                   "5. Key each row/cell from the PDF into the SGML structure.\n"
                   "6. Save and re-validate.",
        "time_est": "15-30 minutes per table",
    },

    "D4_MISSING_IMAGES": {
        "problem": "The source PDF contains image(s) but the SGML has no <GRAPHIC> tags. "
                   "Each image must be represented as <GRAPHIC FILENAME='...'/>.",
        "wrong":   "(image from PDF has no corresponding <GRAPHIC> tag in SGML)",
        "correct":  "<GRAPHIC FILENAME='doc-id_fig1.jpg'/>",
        "steps":   "1. Open the source PDF and identify the image(s) (figure, chart, logo, etc.).\n"
                   "2. For each image, note its position in the document (which section).\n"
                   "3. In the SGML, find the surrounding paragraph text for context.\n"
                   "4. Insert: <GRAPHIC FILENAME='doc-id_figN.jpg'/> at the correct position.\n"
                   "   — Use the document ID and a sequential figure number for the filename.\n"
                   "5. Save and re-validate.",
        "time_est": "5 minutes per image",
    },

    "D4_LOW_SECTION_COVERAGE": {
        "problem": "The SGML has significantly fewer section headings than the source PDF. "
                   "Sections present in the PDF are not represented as <BLOCK>/<TI> in the SGML. "
                   "This usually means entire sections were dropped or merged.",
        "wrong":   "(PDF has 10 sections but SGML only has 3 <TI> headings)",
        "correct":  "<BLOCK1>\n  <TI>Section Title from PDF</TI>\n  <P>Content...</P>\n</BLOCK1>",
        "steps":   "1. Open the source PDF and list all section headings (bold/larger font text).\n"
                   "2. Search for each heading text in the SGML.\n"
                   "3. For missing headings: find the surrounding content in SGML and add "
                   "<BLOCK1><TI>heading</TI>...</BLOCK1> around the relevant paragraphs.\n"
                   "4. Adjust BLOCK level (1/2/3/4) to match the heading depth in the PDF.\n"
                   "5. Save and re-validate.",
        "time_est": "5-10 minutes per missing section",
    },

    # ── L4 Tagging Accuracy (D2) ──────────────────────────────────────────────

    "D2_UNTAGGED_HEADINGS": {
        "problem": "PDF headings detected by font size analysis are not tagged as <TI> in "
                   "the SGML. The description lists the heading text(s) that are missing <TI> tags.",
        "wrong":   "<P>Introduction to Regulatory Framework</P>  ← should be a heading",
        "correct":  "<BLOCK1>\n  <TI>Introduction to Regulatory Framework</TI>\n  ...\n</BLOCK1>",
        "steps":   "1. Read the issue description — it lists the untagged heading text(s).\n"
                   "2. Search for that text in the SGML file.\n"
                   "3. If found inside a <P> tag: wrap it in a BLOCK structure instead:\n"
                   "   Remove the <P> and replace with <BLOCK1><TI>heading text</TI></BLOCK1>.\n"
                   "4. If not found: add a new BLOCK with <TI> at the correct position.\n"
                   "5. Save and re-validate.",
        "time_est": "3-5 minutes per heading",
    },

    "D2_UNTAGGED_BOLD": {
        "problem": "Text spans detected as bold in the PDF are not wrapped in <B> tags in "
                   "the SGML. Bold text in body paragraphs must use <B>...</B>.",
        "wrong":   "<P>The term registrant means any person registered under section 25.</P>",
        "correct":  "<P>The term <B>registrant</B> means any person registered under section 25.</P>",
        "steps":   "1. Open the source PDF and identify bold text spans in body paragraphs.\n"
                   "2. Find the corresponding paragraph in the SGML.\n"
                   "3. Wrap each bold span with <B>...</B> tags.\n"
                   "4. Do NOT use <B> for headings — use <TI> inside a BLOCK instead.\n"
                   "5. Save and re-validate.",
        "time_est": "2-3 minutes per paragraph",
    },

    "D2_UNTAGGED_ITALIC": {
        "problem": "Text spans detected as italic in the PDF are not wrapped in <I> tags in "
                   "the SGML. Italic text in body paragraphs must use <I>...</I>.",
        "wrong":   "<P>See National Instrument 31-103 for details.</P>",
        "correct":  "<P>See <I>National Instrument 31-103</I> for details.</P>",
        "steps":   "1. Open the source PDF and identify italic text spans in body paragraphs.\n"
                   "2. Find the corresponding paragraph in the SGML.\n"
                   "3. Wrap each italic span with <I>...</I> tags.\n"
                   "4. Save and re-validate.",
        "time_est": "2-3 minutes per paragraph",
    },

    # ── L4 Encoding (D6) ─────────────────────────────────────────────────────

    "D6_UNICODE_ENTITIES": {
        "problem": "The SGML contains raw Unicode characters that must be encoded as named "
                   "SGML entities. Common violations: typographic quotes, em/en dashes, accented "
                   "letters, bullet points.",
        "wrong":   "<P>The registrant\u2019s obligation\u2014as defined in section 2\u2014applies.</P>",
        "correct":  "<P>The registrant&rsquo;s obligation&mdash;as defined in section 2&mdash;applies.</P>",
        "steps":   "1. Read the issue description — it specifies which characters are violating.\n"
                   "2. Use Find & Replace in your SGML editor:\n"
                   "   \u2018 (U+2018) \u2192 &lsquo;    \u2019 (U+2019) \u2192 &rsquo;\n"
                   "   \u201c (U+201C) \u2192 &ldquo;    \u201d (U+201D) \u2192 &rdquo;\n"
                   "   \u2013 (U+2013) \u2192 &ndash;    \u2014 (U+2014) \u2192 &mdash;\n"
                   "   \u00e9 (U+00E9) \u2192 &eacute;   \u00e0 (U+00E0) \u2192 &agrave;\n"
                   "   \u2022 (U+2022) \u2192 &bull;     \u2026 (U+2026) \u2192 &hellip;\n"
                   "3. Replace all occurrences (not just the first).\n"
                   "4. Save and re-validate.",
        "time_est": "5-10 minutes (bulk Find & Replace)",
    },

    "D6_BARE_HYPHEN_DASH": {
        "problem": "A bare hyphen (-) is used where an en-dash (&ndash;) or em-dash (&mdash;) "
                   "is appropriate. Common pattern: 'word - word' should use &ndash;.",
        "wrong":   "<P>The period from January - March 2025 applies.</P>",
        "correct":  "<P>The period from January &ndash; March 2025 applies.</P>",
        "steps":   "1. Search for the pattern: space-hyphen-space ( - ) in the SGML file.\n"
                   "2. For date/number ranges: replace ' - ' with ' &ndash; '.\n"
                   "3. For sentence interruptions/parentheticals: replace ' - ' with ' &mdash; '.\n"
                   "4. Tip: use a text editor regex search: \\s-\\s to find all occurrences.\n"
                   "5. Save and re-validate.",
        "time_est": "5 minutes",
    },

    # ── L4 Text Accuracy (D3) ─────────────────────────────────────────────────

    "D3_MISSING_PARAGRAPHS": {
        "problem": "Paragraphs present in the source PDF are not found in the SGML. "
                   "The description lists the missing paragraph text(s). Note: if the warning "
                   "says 'ABBYY extraction gap', the text was lost during PDF-to-DOCX conversion "
                   "and CANNOT be fixed in the SGML editor — it requires re-running ABBYY.",
        "wrong":   "(paragraph text from PDF absent from SGML)",
        "correct":  "<P>The missing paragraph text, fully keyed.</P>",
        "steps":   "1. Check if the issue says 'ABBYY extraction gap' — if so, skip (cannot fix in editor).\n"
                   "2. Otherwise: search for the first few words of the missing paragraph in the PDF.\n"
                   "3. Find the correct position in the SGML (search for neighbouring paragraph text).\n"
                   "4. Insert <P>missing text</P> at the correct location.\n"
                   "5. Preserve any inline tags (<B>, <I>, <EM>) from the PDF formatting.\n"
                   "6. Save and re-validate.",
        "time_est": "2-5 minutes per paragraph",
    },

    # ── L4 DTD / Schema (D7 soft checks) ─────────────────────────────────────

    "D7_ADDDATE_MISMATCH": {
        "problem": "The ADDDATE attribute on <POLIDOC> differs significantly from the date "
                   "found in the source PDF. This is usually a keying date vs publication date "
                   "mismatch — the ADDDATE should be the document's effective/publication date.",
        "wrong":   "<POLIDOC ... ADDDATE='20240607' ...>  ← keying date, not publication date",
        "correct":  "<POLIDOC ... ADDDATE='20190612' ...>  ← publication date from PDF cover page",
        "steps":   "1. Open the source PDF and find the document date (cover page, header, or footer).\n"
                   "2. Format it as YYYYMMDD (e.g., June 12, 2019 → 20190612).\n"
                   "3. In the SGML, find the <POLIDOC ... ADDDATE='...'> opening tag.\n"
                   "4. Replace the ADDDATE value with the correct date.\n"
                   "5. Save and re-validate.",
        "time_est": "2 minutes",
    },

    "D7_LANG_MISMATCH": {
        "problem": "The LANG attribute on <POLIDOC> does not match the detected language of "
                   "the source PDF. French documents must use LANG='FR', English LANG='EN'.",
        "wrong":   "<POLIDOC ... LANG='EN' ...>  ← but document is French",
        "correct":  "<POLIDOC ... LANG='FR' ...>",
        "steps":   "1. Open the source PDF and confirm the document language (first paragraph).\n"
                   "2. In the SGML, find the <POLIDOC ... LANG='...'> opening tag.\n"
                   "3. Change LANG to 'EN' or 'FR' as appropriate.\n"
                   "4. Save and re-validate.",
        "time_est": "1 minute",
    },

    # ── L4 Ordering (D5) ─────────────────────────────────────────────────────

    "D5_SECTION_ORDERING": {
        "problem": "Sections appear in a different order in the SGML compared to the source PDF. "
                   "The validator detected that a section heading appearing later in the PDF "
                   "was placed earlier in the SGML (or vice versa).",
        "wrong":   "(Section B appears before Section A in SGML, but PDF has A before B)",
        "correct":  "(SGML sections follow the same top-to-bottom order as the PDF)",
        "steps":   "1. Note: for two-column PDFs this is often a false positive — check the PDF layout.\n"
                   "2. Open the source PDF and note the correct section order.\n"
                   "3. In the SGML, identify the misplaced BLOCK sections.\n"
                   "4. Cut the misplaced BLOCK and paste it in the correct position.\n"
                   "5. Verify surrounding content still makes sense after the move.\n"
                   "6. Save and re-validate.",
        "time_est": "10-15 minutes",
    },
}


# ── Lookup function ───────────────────────────────────────────────────────────

def get_fix_template(key: str) -> Optional[dict]:
    """Return the fix template dict for the given key, or None if not found."""
    return _TEMPLATES.get(key)


# ── Keyword-based auto-matching ───────────────────────────────────────────────

# Maps (category, keyword_in_description) → template key
# Checked in order; first match wins.
_AUTO_MATCH_RULES: list[tuple[str, str, str]] = [
    # (category,          keyword_fragment_in_description,   template_key)
    ("dtd_schema",        "LANG",                            "POLIDOC_MISSING_LANG"),
    ("dtd_schema",        "LABEL",                           "POLIDOC_MISSING_LABEL"),
    ("dtd_schema",        "ADDDATE",                         "POLIDOC_MISSING_ADDDATE"),
    ("dtd_schema",        "YYYYMMDD",                        "POLIDOC_ADDDATE_FORMAT"),
    ("dtd_schema",        "POLIDOC",                         "POLIDOC_MISSING_ROOT"),
    ("tag_nesting",       "nested inside",                   "BLOCK_HIERARCHY_INVERSION"),
    ("tag_nesting",       "SGMLTBL found outside",           "SGMLTBL_OUTSIDE_TABLE"),
    ("tag_nesting",       "P> found inside <TBLCELL",        "P_IN_TBLCELL"),
    ("tag_nesting",       "ITEM> found",                     "EMPTY_N_TAG"),       # fallback
    ("table_structure",   "does not contain <SGMLTBL",       "TABLE_MISSING_SGMLTBL"),
    ("table_structure",   "P> found inside <TBLCELL",        "P_IN_TBLCELL"),
    ("table_structure",   "SGMLTBL> found outside",          "SGMLTBL_OUTSIDE_TABLE"),
    ("table_structure",   "missing <TBLBODY",                "SGMLTBL_MISSING_TBLBODY"),
    ("entity_handling",   "bare '<'",                        "BARE_LT_CHARACTERS"),
    ("entity_handling",   "bare '&'",                        "BARE_AMP_CHARACTERS"),
    ("entity_handling",   "bare '&",                         "BARE_AMP_CHARACTERS"),
    ("entity_handling",   "Unknown entit",                   "UNKNOWN_ENTITY"),
    ("content_rules",     "empty <N>",                       "EMPTY_N_TAG"),
    ("content_rules",     "Empty N",                         "EMPTY_N_TAG"),
    ("content_rules",     "empty <TI>",                      "EMPTY_TI_TAG"),
    ("content_rules",     "Empty TI",                        "EMPTY_TI_TAG"),
    ("content_rules",     "end with a period",               "TI_ENDS_WITH_PERIOD"),
    ("content_rules",     "ends with period",                "TI_ENDS_WITH_PERIOD"),
    ("content_rules",     "trailing period",                 "TI_ENDS_WITH_PERIOD"),
    ("content_rules",     "URL",                             "URL_NOT_IN_LINE"),
    ("legal_rules",       "POLIDENT> element",               "POLIDENT_MISSING"),
    ("legal_rules",       "POLIDENT missing <N>",            "POLIDENT_MISSING_N"),
    ("legal_rules",       "POLIDENT missing <TI>",           "POLIDENT_MISSING_TI"),
    ("legal_rules",       "DEF",                             "DEF_MISSING_DEFP"),
    ("graphics",          "FILENAME attribute",              "GRAPHIC_MISSING_FILENAME"),
    ("graphics",          "does not match",                  "GRAPHIC_WRONG_FILENAME_FORMAT"),
    # L1 content
    ("text_completeness", "Catastrophic",                    "CATASTROPHIC_CONTENT_LOSS"),
    ("text_completeness", "low paragraph coverage",          "LOW_PARAGRAPH_COVERAGE"),
    ("text_completeness", "Very low paragraph",              "LOW_PARAGRAPH_COVERAGE"),
    ("text_completeness", "paragraphs from PDF",             "LOW_PARAGRAPH_COVERAGE"),
    ("section_completeness", "heading",                      "MISSING_HEADINGS"),
    ("table_completeness",   "table",                        "MISSING_TABLES"),
    # L4 D4 completeness
    ("completeness",  "table",                               "D4_MISSING_TABLES"),
    ("completeness",  "image",                               "D4_MISSING_IMAGES"),
    ("completeness",  "graphic",                             "D4_MISSING_IMAGES"),
    ("completeness",  "section coverage",                    "D4_LOW_SECTION_COVERAGE"),
    ("completeness",  "heading",                             "D4_LOW_SECTION_COVERAGE"),
    # L4 D2 tagging accuracy
    ("tagging_accuracy", "heading",                          "D2_UNTAGGED_HEADINGS"),
    ("tagging_accuracy", "<TI>",                             "D2_UNTAGGED_HEADINGS"),
    ("tagging_accuracy", "bold",                             "D2_UNTAGGED_BOLD"),
    ("tagging_accuracy", "<B>",                              "D2_UNTAGGED_BOLD"),
    ("tagging_accuracy", "italic",                           "D2_UNTAGGED_ITALIC"),
    ("tagging_accuracy", "<I>",                              "D2_UNTAGGED_ITALIC"),
    ("tagging_accuracy", "image",                            "D4_MISSING_IMAGES"),
    ("tagging_accuracy", "graphic",                          "D4_MISSING_IMAGES"),
    # L4 D6 encoding
    ("encoding",      "unicode",                             "D6_UNICODE_ENTITIES"),
    ("encoding",      "entity",                              "D6_UNICODE_ENTITIES"),
    ("encoding",      "hyphen",                              "D6_BARE_HYPHEN_DASH"),
    ("encoding",      "dash",                                "D6_BARE_HYPHEN_DASH"),
    # L4 D3 text accuracy
    ("text_accuracy", "paragraph",                           "D3_MISSING_PARAGRAPHS"),
    ("text_accuracy", "missing",                             "D3_MISSING_PARAGRAPHS"),
    # L4 D7 metadata
    ("metadata",      "ADDDATE",                             "D7_ADDDATE_MISMATCH"),
    ("metadata",      "date",                                "D7_ADDDATE_MISMATCH"),
    ("metadata",      "LANG",                                "D7_LANG_MISMATCH"),
    ("metadata",      "language",                            "D7_LANG_MISMATCH"),
    # L4 D5 ordering
    ("ordering",      "order",                               "D5_SECTION_ORDERING"),
    ("ordering",      "reorder",                             "D5_SECTION_ORDERING"),
    ("ordering",      "sequence",                            "D5_SECTION_ORDERING"),
]


def _find_template_key(category: str, description: str) -> Optional[str]:
    """Return the best-matching template key for an issue, or None."""
    desc_lower = description.lower()
    for cat, keyword, key in _AUTO_MATCH_RULES:
        if cat == category and keyword.lower() in desc_lower:
            return key
    # Second pass: ignore category, match on description only
    for cat, keyword, key in _AUTO_MATCH_RULES:
        if keyword.lower() in desc_lower:
            return key
    return None


def enrich_issue(issue: dict) -> dict:
    """
    Add a 'fix_template' key to an issue dict if a matching template exists.
    Modifies the dict in-place and also returns it.

    Parameters
    ----------
    issue : dict with at least 'category' and 'description' keys.

    Returns
    -------
    The same dict, optionally enriched with 'fix_template'.
    """
    category    = issue.get("category", "")
    description = issue.get("description", "")
    key = _find_template_key(category, description)
    if key:
        tmpl = _TEMPLATES[key]
        issue["fix_template"] = {
            "template_id": key,
            "problem":     tmpl["problem"],
            "wrong":       tmpl["wrong"],
            "correct":     tmpl["correct"],
            "steps":       tmpl["steps"],
            "time_est":    tmpl["time_est"],
        }
    return issue


def enrich_issues(issues: list[dict]) -> list[dict]:
    """Enrich a list of issue dicts with fix templates. Returns the same list."""
    for issue in issues:
        enrich_issue(issue)
    return issues
