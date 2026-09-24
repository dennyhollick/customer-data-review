"""Select evidence by exact source spans instead of retyping quotations.

The model still owns interpretation. Code owns source coverage, offsets, quote
bytes and header-supported speaker names. Literal provenance is not relevance.
"""
import copy
import hashlib
import re

HEADER = re.compile(r"(?m)^[ \t]*\d{1,2}:\d{2}(?::\d{2})?[ \t]+-[ \t]+(?P<speaker>[^\r\n]+)(?:\r?\n|$)")
TIMESTAMP_LINE = re.compile(r"(?m)^[ \t]*\d{1,2}:\d{2}(?::\d{2})?[^\r\n]*")
MAX_WORDS = 32


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def split_source(file_id, text):
    headers = list(HEADER.finditer(text))
    if not headers:
        raise ValueError(f'{file_id}: no supported speaker turns; full source requires another input path, not silent omission.')
    starts = {h.start() for h in headers}
    if any(m.start() not in starts for m in TIMESTAMP_LINE.finditer(text)):
        raise ValueError(f'{file_id}: ambiguous timestamp/header coverage; source cannot be silently classified as no evidence.')
    packet = {'source_sha256': digest(text), 'preamble': text[:headers[0].start()], 'turns': []}
    index = {'source_sha256': digest(text), 'text': text, 'spans': {}}
    sequence = 0
    for turn_number, header in enumerate(headers):
        end = headers[turn_number + 1].start() if turn_number + 1 < len(headers) else len(text)
        speaker = re.sub(r'\s*\([^)]*\)\s*$', '', header.group('speaker')).strip()
        if not speaker:
            raise ValueError(f'{file_id}: unsupported empty speaker header; preserve source as incomplete.')
        turn = {'header': header.group(), 'spans': []}
        words = list(re.finditer(r'\S+', text[header.end():end]))
        cursor, word = header.end(), 0
        if not words:
            # Whitespace-only turns remain present for exact full reconstruction.
            turn['empty_body'] = text[cursor:end]
        while word < len(words):
            limit = min(word + MAX_WORDS, len(words))
            if limit < len(words):
                boundaries = [i + 1 for i in range(word + 15, limit)
                              if re.search(r'[.!?][\"\u201d\u2019\')]*$', words[i].group())]
                if boundaries:
                    limit = boundaries[-1]
            stop = header.end() + words[limit].start() if limit < len(words) else end
            sequence += 1
            span_id = f'{file_id}__s{sequence:04d}'
            body = text[cursor:stop]
            turn['spans'].append({'span_id': span_id, 'words': len(body.split()), 'text': body})
            index['spans'][span_id] = {'start': cursor, 'end': stop, 'turn': turn_number,
                                      'sequence': sequence, 'speaker': speaker,
                                      'header_start': header.start(), 'header_end': header.end()}
            cursor, word = stop, limit
        packet['turns'].append(turn)
    rebuilt = packet['preamble'] + ''.join(t['header'] + ''.join(s['text'] for s in t['spans'])
                    + t.get('empty_body', '') for t in packet['turns'])
    if rebuilt != text:
        raise ValueError('Source span coverage failed; no model dispatch.')
    return packet, index


def prepare(payload):
    """Replace full text with a lossless representation, preserving task metadata."""
    result, indexes = copy.deepcopy(payload), {}
    for item in result['items']:
        file_id = item['file_id']
        if file_id in indexes:
            raise ValueError('Duplicate source in span task.')
        packet, index = split_source(file_id, item.pop('source_text'))
        item['source_spans'] = packet
        index['source_descriptor'] = item['quote_source_descriptor']
        indexes[file_id] = index
    result['instruction'] = ('Read all preamble, headers and ordered spans in every file. Return the supplied JSON schema. '
        'For each mention select1-3consecutive span IDs from the same speaker turn (each <=32words; '
        'combined quote must be6-100words). Read adjacent spans; preserve governing negations, conditions '
        'and claim ownership. If useful evidence is outside a single quote, retain distinct grounded mentions. '
        'Code supplies exact quote text and actual speaker name; do not invent roles or assume customer ownership. '
        'Preamble has no verified speaker and is context only. No silent source omission or fixed mention quota.')
    return result, indexes


def hydrate(answer, indexes):
    """Resolve one verified original substring per selection; never fuzzy-match."""
    result = copy.deepcopy(answer)
    ids = [row['file_id'] for row in result['sources']]
    if len(ids) != len(set(ids)) or set(ids) != set(indexes):
        raise ValueError('Span answer must cover exactly all assigned source IDs.')
    for row in result['sources']:
        index = indexes[row['file_id']]
        text = index['text']
        if digest(text) != index['source_sha256']:
            raise ValueError('Stale source hash in span mapping.')
        if index['spans'] != split_source(row['file_id'], text)[1]['spans']:
            raise ValueError('Changed code-owned span mapping or source offsets.')
        for mention in row['mentions']:
            selected = mention.pop('quote_span_ids')
            if not isinstance(selected, list) or not 1 <= len(selected) <= 3 or len(set(selected)) != len(selected):
                raise ValueError('Select1-3distinct ordered quote spans.')
            try:
                spans = [index['spans'][sid] for sid in selected]
            except KeyError as exc:
                raise ValueError('Unknown or cross-source quotation span.') from exc
            if len({s['turn'] for s in spans}) != 1 or any(
                    right['sequence'] != left['sequence'] + 1 or left['end'] != right['start']
                    for left, right in zip(spans, spans[1:])):
                raise ValueError('Quote spans must be consecutive, ordered and within one speaker turn.')
            start, end = spans[0]['start'], spans[-1]['end']
            if not 0 <= start < end <= len(text) or start < spans[0]['header_end']:
                raise ValueError('Invalid source offsets or quotation crosses a speaker header.')
            quote = text[start:end].strip()
            if not 6 <= len(quote.split()) <= 100:
                raise ValueError('Selected source quotation must contain6-100words; never truncate it.')
            mention['quote'] = quote
            mention['quote_attribution'] = f"{spans[0]['speaker']} ({index['source_descriptor']})"
    return result


def hydrate_batch(answer, indexes):
    """Keep valid sources when another source has a bad span selection."""
    ids = [row['file_id'] for row in answer['sources']]
    if len(ids) != len(set(ids)) or set(ids) != set(indexes):
        raise ValueError('Span answer must cover exactly all assigned source IDs.')
    good, errors = [], {}
    for row in answer['sources']:
        fid = row['file_id']
        try:
            good.extend(hydrate({'sources': [row]}, {fid: indexes[fid]})['sources'])
        except (ValueError, KeyError, TypeError) as exc:
            errors[fid] = str(exc)
    return {'sources': good, '_source_errors': errors}
