"""Operator guidance for confident snapshot-class winners (no hardware I/O)."""

import math


CLASS_DIAGNOSTICS = {
    '50_CARD_NOT_FULLY_INSERTED': ('MCIO_NOT_FULLY_INSERTED', 'Please fully insert MCIO card'),
    '50_COVER_CLOSED': ('MCIO_MISSING', 'Please insert MCIO card'),
    '50_COVER_OPEN_CARD_PRESENT': ('COVER_OPEN', 'Please close the cover'),
    '50_PRODUCT_LOADED': ('MCIO_MISSING_COVER_OPEN', 'Insert MCIO card and close cover'),
    'EMPTY_FIXTURE': ('PRODUCT_MISSING', 'Place product in mold'),
    '50_COVER_CLOSED_CARD_PRESENT': ('TARGET_MATCH', 'Confirming match'),
}
DIAGNOSTIC_MESSAGES = dict(CLASS_DIAGNOSTICS.values())
NG_DIAGNOSTICS = set(DIAGNOSTIC_MESSAGES) - {'TARGET_MATCH'}


def canonical_class(name):
    name = str(name)
    return name[:-6] if name.endswith('_CROOP') else name


def winner_diagnostic(class_scores, threshold, min_gap):
    """Rank all classes, but name a condition only above both confidence limits."""
    ranked = sorted(((name, float(score)) for name, score in class_scores.items()
                     if isinstance(score, (int, float)) and not isinstance(score, bool)
                     and math.isfinite(score)), key=lambda item: item[1], reverse=True)
    result = dict(winning_state=None, winning_score=None, winning_gap=None,
                  diagnostic_code=None)
    if not ranked:
        return result
    name, score = ranked[0]
    gap = score - ranked[1][1] if len(ranked) > 1 else None
    result.update(winning_state=name, winning_score=score, winning_gap=gap)
    # Missing competitors and exact ties cannot establish a distinct winner.
    if gap is not None and gap > 0 and score >= threshold and gap + 1e-12 >= min_gap:
        entry = CLASS_DIAGNOSTICS.get(canonical_class(name))
        result['diagnostic_code'] = entry[0] if entry else None
    return result


def active_diagnostic(state, stale=False):
    """Only active probes and committed failures may describe a reject condition."""
    if stale or state.get('status_stale') is True or state.get('inspection_mode') != 'single_snapshot_cover_card':
        return None
    phase = state.get('state')
    armed = (state.get('auto_capture') or {}).get('armed') is True
    if phase == 'NOT_MATCHED' or phase == 'WAITING_CAPTURE' and armed:
        code = (state.get('matching') or {}).get('diagnostic_code')
        return code if code in DIAGNOSTIC_MESSAGES else None
    return None
