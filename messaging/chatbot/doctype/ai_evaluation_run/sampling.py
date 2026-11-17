"""Versioned, reproducible reservoir sampling over stable catalogue ID pages."""
import hashlib

from messaging.chatbot.doctype.ai_evaluation_case_template import queries
from messaging.chatbot.doctype.ai_evaluation_case_template.ai_evaluation_case_template import canonical, reject

ALGORITHM = 'sha256-reservoir-union-v2'


class RandomStream:
    """A specified integer stream avoids dependence on Python's random implementation."""
    def __init__(self, seed, stratum):
        self.key = canonical([ALGORITHM, seed, stratum]).encode()
        self.counter = 0

    def below(self, bound):
        # Rejection avoids modulo bias, including when the case bank grows.
        limit = 2**256 - 2**256 % bound
        while True:
            value = int.from_bytes(hashlib.sha256(self.key + b':' + str(self.counter).encode()).digest(), 'big')
            self.counter += 1
            if value < limit:
                return value % bound


def select_cases(case_template, language, group, count, seed, *, minimum_parameters=1):
    available = queries.count_cases(case_template, language=language, group=group, minimum_parameters=minimum_parameters)
    if available < count:
        reject('Insufficient template coverage: {0}/{1}/{2}; required {3}, available {4}.'.format(
            case_template, language, group, count, available))
    stream = RandomStream(seed, [case_template, language, group, minimum_parameters])
    chosen, seen, start = [], 0, 0
    while True:
        rows = queries.list_cases(case_template, language=language, group=group, start=start, page_length=500, minimum_parameters=minimum_parameters)
        if not rows:
            break
        for row in rows:
            seen += 1
            if len(chosen) < count:
                chosen.append(dict(row))
            else:
                index = stream.below(seen)
                if index < count:
                    chosen[index] = dict(row)
        start += len(rows)
    if seen != available or len(chosen) != count:
        reject('The template catalogue changed during selection. Request a new evaluation.')
    return sorted(chosen, key=lambda r: r['name'])


def select_strata(strata, seed):
    """One execution per case; each selected stratum retains its exact contribution."""
    selected = {}
    for stratum in sorted(strata, key=lambda s: (s['case_template'], s['language'], s['group'], s.get('minimum_parameters', 1))):
        if not stratum['sufficient']:
            continue
        key = [stratum['group'], stratum.get('minimum_parameters', 1)]
        for item in select_cases(stratum['case_template'], stratum['language'], stratum['group'],
            stratum['sample_count'], seed, minimum_parameters=stratum.get('minimum_parameters', 1)):
            name = item['name']
            if name not in selected:
                selected[name] = dict(item, coverage_strata=[])
            if selected[name]['definition_revision'] != item['definition_revision']:
                reject('The template catalogue changed during selection. Request a new evaluation.')
            selected[name]['coverage_strata'].append(key)
    return sorted(selected.values(), key=lambda item: (item['case_template'], item['name']))
