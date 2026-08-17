# MarkVector — supported formats

**Contract version:** `1.0.0` · served live at `GET /api/formats`

This document is generated from `packages/core/formats.py`, which is checked
against the live parser registry by `tests/test_formats.py` — a format that is
accepted but undocumented fails CI. So this list cannot drift from what the
software actually reads.

**Unit of citation** is the part to design around: it says what one retrieved
passage corresponds to in the original document.

## Parsed natively

### Plain text and Markdown

**Extensions:** `.txt`, `.text`, `.md`, `.markdown`  
**Unit of citation:** a section under its nearest heading

Markdown headings are used as-is. Plain text is scanned for numbered and spoken headings so a long document still has structure to split on.

### PDF

**Extensions:** `.pdf`  
**Unit of citation:** a section under its nearest heading, carrying its page number

Both text-native and scanned PDFs. A scanned PDF is transcribed page by page with a vision model, capped at MAX_TRANSCRIBE_PAGES (200) — a truncated transcription says so on the document rather than being quietly short. Page pictures are rendered on demand up to 5,000 pages, and figures on a cited page are cut out and shown with the answer.

*Not carried into the index:* page furniture (running heads and folios).

### Word

**Extensions:** `.docx`  
**Unit of citation:** a section under its nearest heading

Paragraphs and tables are read in document order, so a document whose substance is in its tables is not silently rearranged. Tables keep their columns as Markdown rows.

*Not carried into the index:* fonts and colours, comments, tracked changes, page numbers.

### PowerPoint

**Extensions:** `.pptx`  
**Unit of citation:** one slide — title, body and speaker notes together

Slides are read in slide order, not archive order, so slide 10 does not land between slide 1 and slide 2. Speaker notes are kept with their slide, because they frequently hold the argument the slide only gestures at.

*Not carried into the index:* slide masters and layouts, animations, images (no alt text in OOXML).

### Excel

**Extensions:** `.xlsx`, `.xlsm`  
**Unit of citation:** a sheet, as a table with its header row

Each sheet is kept separate and its grid preserved, so a value stays attached to the column heading that gives it meaning. Dates are read as dates rather than as Excel's serial numbers, and percentages are not left two orders of magnitude out.

*Not carried into the index:* formulas (the computed value is kept), charts, cell formatting.

### CSV and TSV

**Extensions:** `.csv`, `.tsv`  
**Unit of citation:** a table with its header row

The delimiter is sniffed rather than assumed.

### HTML

**Extensions:** `.html`, `.htm`, `.xhtml`  
**Unit of citation:** a section under its nearest heading

Headings, lists and tables survive; tables keep their columns. A page whose content is rendered by JavaScript has no text to read and is refused with that reason, rather than indexing as an empty success — save it from the browser after it loads, or upload it as PDF.

*Not carried into the index:* script and style contents, navigation, headers, footers and sidebars.

### Images

**Extensions:** `.png`, `.jpg`, `.jpeg`, `.webp`, `.gif`, `.bmp`, `.tif`, `.tiff`  
**Unit of citation:** the picture, described by a vision model

An image has no text to extract, so it is read as a picture and its description is what becomes searchable. It is titled by its filename, never by the model's description of it.

## Not supported — convert before uploading

| What | Guidance |
|---|---|
| `.doc, .xls, .ppt` | the pre-2007 binary Office formats — convert to their x variants |
| `.rtf` | convert to DOCX or PDF |
| `.epub` | convert to PDF |
| `.eml, .msg` | email; extract the body and attachments and upload those |
| `.zip` | archives are not unpacked; upload the files inside |
| `audio and video` | no transcription pipeline |

## When a file cannot be parsed

A file that cannot be parsed becomes an item at status 'failed' with the reason in metadata.failure, never a silent empty ingest. Re-uploading it is the retry. A failed re-upload never replaces a version that indexed cleanly.

## Versioning

The version moves when the SET of formats changes, or when a family's unit of
citation changes. It does not move when a parser gets better at the same job.
Pin against it if you are building conversion logic on your side.
