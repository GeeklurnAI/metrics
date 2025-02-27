# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import re
from collections import Counter
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import torch
from torch import Tensor, tensor
from typing_extensions import Literal

from torchmetrics.utilities.imports import _NLTK_AVAILABLE

__doctest_requires__ = {("rouge_score", "_rouge_score_update"): ["nltk"]}

ALLOWED_ROUGE_KEYS: Dict[str, Union[int, str]] = {
    "rouge1": 1,
    "rouge2": 2,
    "rouge3": 3,
    "rouge4": 4,
    "rouge5": 5,
    "rouge6": 6,
    "rouge7": 7,
    "rouge8": 8,
    "rouge9": 9,
    "rougeL": "L",
    "rougeLsum": "Lsum",
}
ALLOWED_ACCUMULATE_VALUES = ("avg", "best")


def _add_newline_to_end_of_each_sentence(x: str) -> str:
    """This was added to get rougeLsum scores matching published rougeL scores for BART and PEGASUS."""
    if not _NLTK_AVAILABLE:
        raise ModuleNotFoundError("ROUGE-Lsum calculation requires that `nltk` is installed. Use `pip install nltk`.")
    import nltk

    nltk.download("punkt", quiet=True, force=False)

    re.sub("<n>", "", x)  # remove pegasus newline char
    return "\n".join(nltk.sent_tokenize(x))


def _compute_metrics(hits_or_lcs: int, pred_len: int, target_len: int) -> Dict[str, Tensor]:
    """This computes precision, recall and F1 score based on hits/lcs, and the length of lists of tokenizer
    predicted and target sentences.

    Args:
        hits_or_lcs:
            A number of matches or a length of the longest common subsequence.
        pred_len:
            A length of a tokenized predicted sentence.
        target_len:
            A length of a tokenized target sentence.
    """
    precision = hits_or_lcs / pred_len
    recall = hits_or_lcs / target_len
    if precision == recall == 0.0:
        return dict(precision=tensor(0.0), recall=tensor(0.0), fmeasure=tensor(0.0))

    fmeasure = 2 * precision * recall / (precision + recall)
    return dict(precision=tensor(precision), recall=tensor(recall), fmeasure=tensor(fmeasure))


def _lcs(pred_tokens: Sequence[str], target_tokens: Sequence[str]) -> int:
    """Common DP algorithm to compute the length of the longest common subsequence.

    Args:
        pred_tokens:
            A tokenized predicted sentence.
        target_toknes:
            A tokenized target sentence.
    """
    LCS = [[0] * (len(pred_tokens) + 1) for _ in range(len(target_tokens) + 1)]
    for i in range(1, len(target_tokens) + 1):
        for j in range(1, len(pred_tokens) + 1):
            if target_tokens[i - 1] == pred_tokens[j - 1]:
                LCS[i][j] = LCS[i - 1][j - 1] + 1
            else:
                LCS[i][j] = max(LCS[i - 1][j], LCS[i][j - 1])
    return LCS[-1][-1]


def _normalize_and_tokenize_text(text: str, stemmer: Optional[Any] = None) -> Sequence[str]:
    """Rouge score should be calculated only over lowercased words and digits. Optionally, Porter stemmer can be
    used to strip word suffixes to improve matching. The text normalization follows the implemantion from `Rouge
    score_Text Normalizition`_

    Args:
        text:
            An input sentence.
        stemmer:
            Porter stemmer instance to strip word suffixes to improve matching.
    """
    # Replace any non-alpha-numeric characters with spaces.
    text = re.sub(r"[^a-z0-9]+", " ", text.lower())

    tokens = re.split(r"\s+", text)
    if stemmer:
        # Only stem words more than 3 characters long.
        tokens = [stemmer.stem(x) if len(x) > 3 else x for x in tokens]

    # One final check to drop any empty or invalid tokens.
    tokens = [x for x in tokens if (isinstance(x, str) and re.match(r"^[a-z0-9]+$", x))]

    return tokens


def _rouge_n_score(pred: Sequence[str], target: Sequence[str], n_gram: int) -> Dict[str, Tensor]:
    """This computes precision, recall and F1 score for the Rouge-N metric.

    Args:
        pred:
            A predicted sentence.
        target:
            A target sentence.
        n_gram:
            N-gram overlap.
    """

    def _create_ngrams(tokens: Sequence[str], n: int) -> Counter:
        ngrams: Counter = Counter()
        for ngram in (tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1)):
            ngrams[ngram] += 1
        return ngrams

    pred_ngrams, target_ngrams = _create_ngrams(pred, n_gram), _create_ngrams(target, n_gram)
    pred_len, target_len = sum(pred_ngrams.values()), sum(target_ngrams.values())
    if 0 in (pred_len, target_len):
        return dict(precision=tensor(0.0), recall=tensor(0.0), fmeasure=tensor(0.0))

    # It is sufficient to take a set(pred_tokenized) for hits count as we consider intersenction of pred & target
    hits = sum(min(pred_ngrams[w], target_ngrams[w]) for w in set(pred_ngrams))
    return _compute_metrics(hits, max(pred_len, 1), max(target_len, 1))


def _rouge_l_score(pred: Sequence[str], target: Sequence[str]) -> Dict[str, Tensor]:
    """This computes precision, recall and F1 score for the Rouge-L or Rouge-LSum metric.

    Args:
        pred:
            A predicted sentence.
        target:
            A target sentence.
    """
    pred_len, target_len = len(pred), len(target)
    if 0 in (pred_len, target_len):
        return dict(precision=tensor(0.0), recall=tensor(0.0), fmeasure=tensor(0.0))

    lcs = _lcs(pred, target)
    return _compute_metrics(lcs, pred_len, target_len)


def _rouge_score_update(
    preds: Sequence[str],
    target: Sequence[Sequence[str]],
    rouge_keys_values: List[Union[int, str]],
    accumulate: str,
    stemmer: Optional[Any] = None,
) -> Dict[Union[int, str], List[Dict[str, Tensor]]]:
    """Update the rouge score with the current set of predicted and target sentences.

    Args:
        preds:
            An iterable of predicted sentences.
        target:
            An iterable of iterable of target sentences.
        rouge_keys_values:
            List of N-grams/'L'/'Lsum' arguments.
        accumulate:
            Useful incase of multi-reference rouge score.
            ``avg`` takes the avg of all references with respect to predictions
            ``best`` takes the best fmeasure score obtained between prediction and multiple corresponding references.
            Allowed values are ``avg`` and ``best``.
        stemmer:
            Porter stemmer instance to strip word suffixes to improve matching.

    Example:
        >>> preds = "My name is John".split()
        >>> target = "Is your name John".split()
        >>> from pprint import pprint
        >>> score = _rouge_score_update(preds, target, rouge_keys_values=[1, 2, 3, 'L'], accumulate='best')
        >>> pprint(score)
        {1: [{'fmeasure': tensor(0.), 'precision': tensor(0.), 'recall': tensor(0.)},
             {'fmeasure': tensor(0.), 'precision': tensor(0.), 'recall': tensor(0.)},
             {'fmeasure': tensor(0.), 'precision': tensor(0.), 'recall': tensor(0.)},
             {'fmeasure': tensor(0.), 'precision': tensor(0.), 'recall': tensor(0.)}],
         2: [{'fmeasure': tensor(0.), 'precision': tensor(0.), 'recall': tensor(0.)},
             {'fmeasure': tensor(0.), 'precision': tensor(0.), 'recall': tensor(0.)},
             {'fmeasure': tensor(0.), 'precision': tensor(0.), 'recall': tensor(0.)},
             {'fmeasure': tensor(0.), 'precision': tensor(0.), 'recall': tensor(0.)}],
         3: [{'fmeasure': tensor(0.), 'precision': tensor(0.), 'recall': tensor(0.)},
             {'fmeasure': tensor(0.), 'precision': tensor(0.), 'recall': tensor(0.)},
             {'fmeasure': tensor(0.), 'precision': tensor(0.), 'recall': tensor(0.)},
             {'fmeasure': tensor(0.), 'precision': tensor(0.), 'recall': tensor(0.)}],
         'L': [{'fmeasure': tensor(0.), 'precision': tensor(0.), 'recall': tensor(0.)},
               {'fmeasure': tensor(0.), 'precision': tensor(0.), 'recall': tensor(0.)},
               {'fmeasure': tensor(0.), 'precision': tensor(0.), 'recall': tensor(0.)},
               {'fmeasure': tensor(0.), 'precision': tensor(0.), 'recall': tensor(0.)}]}
    """
    results: Dict[Union[int, str], List[Dict[str, Tensor]]] = {rouge_key: [] for rouge_key in rouge_keys_values}

    for pred_raw, target_raw in zip(preds, target):
        result_inner: Dict[Union[int, str], Dict[str, Tensor]] = {rouge_key: {} for rouge_key in rouge_keys_values}
        result_avg: Dict[Union[int, str], List[Dict[str, Tensor]]] = {rouge_key: [] for rouge_key in rouge_keys_values}
        list_results = []
        pred = _normalize_and_tokenize_text(pred_raw, stemmer)
        pred_Lsum = _normalize_and_tokenize_text(_add_newline_to_end_of_each_sentence(pred_raw), stemmer)

        for target_raw_inner in target_raw:
            tgt = _normalize_and_tokenize_text(target_raw_inner, stemmer)

            if "Lsum" in rouge_keys_values:
                # rougeLsum expects "\n" separated sentences within a summary
                target_Lsum = _normalize_and_tokenize_text(
                    _add_newline_to_end_of_each_sentence(target_raw_inner), stemmer
                )

            for rouge_key in rouge_keys_values:
                if isinstance(rouge_key, int):
                    score = _rouge_n_score(pred, tgt, rouge_key)
                else:
                    score = _rouge_l_score(
                        pred if rouge_key != "Lsum" else pred_Lsum,
                        tgt if rouge_key != "Lsum" else target_Lsum,
                    )
                result_inner[rouge_key] = score
                result_avg[rouge_key].append(score)
            list_results.append(result_inner.copy())

        if accumulate == "best":
            key_curr = rouge_keys_values[0]
            all_fmeasure = torch.tensor([v[key_curr]["fmeasure"] for v in list_results])
            highest_idx = int(torch.argmax(all_fmeasure).item())

            for rouge_key in rouge_keys_values:
                results[rouge_key].append(list_results[highest_idx][rouge_key])

        elif accumulate == "avg":
            new_result_avg: Dict[Union[int, str], Dict[str, Tensor]] = {
                rouge_key: {} for rouge_key in rouge_keys_values
            }
            for rouge_key, metrics in result_avg.items():
                _dict_metric_score_batch: Dict[str, List[Tensor]] = {}
                for metric in metrics:
                    for _type, value in metric.items():
                        if _type not in _dict_metric_score_batch:
                            _dict_metric_score_batch[_type] = []
                        _dict_metric_score_batch[_type].append(value)

                new_result_avg[rouge_key] = {
                    _type: torch.tensor(_dict_metric_score_batch[_type]).mean() for _type in _dict_metric_score_batch
                }

            for rouge_key in rouge_keys_values:
                results[rouge_key].append(new_result_avg[rouge_key])

    return results


def _rouge_score_compute(sentence_results: Dict[str, List[Tensor]]) -> Dict[str, Tensor]:
    """Compute the combined ROUGE metric for all the input set of predicted and target sentences.

    Args:
        sentence_results:
            Rouge-N/Rouge-L/Rouge-LSum metrics calculated for single sentence.
    """
    results: Dict[str, Tensor] = {}
    # Obtain mean scores for individual rouge metrics
    if sentence_results == {}:
        return results

    for rouge_key, scores in sentence_results.items():
        results[rouge_key] = torch.tensor(scores).mean()

    return results


def rouge_score(
    preds: Union[str, Sequence[str]],
    target: Union[str, Sequence[str], Sequence[Sequence[str]]],
    accumulate: Literal["avg", "best"] = "best",
    use_stemmer: bool = False,
    rouge_keys: Union[str, Tuple[str, ...]] = ("rouge1", "rouge2", "rougeL", "rougeLsum"),  # type: ignore
) -> Dict[str, Tensor]:
    """Calculate `Calculate Rouge Score`_ , used for automatic summarization.

    Args:
        preds:
            An iterable of predicted sentences or a single predicted sentence.
        target:
            An iterable of iterables of target sentences or an iterable of target sentences or a single target sentence.
        accumulate:
            Useful incase of multi-reference rouge score.
            - ``avg`` takes the avg of all references with respect to predictions
            - ``best`` takes the best fmeasure score obtained between prediction and multiple corresponding references.
        use_stemmer:
            Use Porter stemmer to strip word suffixes to improve matching.
        rouge_keys:
            A list of rouge types to calculate.
            Keys that are allowed are ``rougeL``, ``rougeLsum``, and ``rouge1`` through ``rouge9``.

    Return:
        Python dictionary of rouge scores for each input rouge key.

    Example:
        >>> from torchmetrics.functional.text.rouge import rouge_score
        >>> preds = "My name is John"
        >>> target = "Is your name John"
        >>> from pprint import pprint
        >>> pprint(rouge_score(preds, target))
        {'rouge1_fmeasure': tensor(0.7500),
         'rouge1_precision': tensor(0.7500),
         'rouge1_recall': tensor(0.7500),
         'rouge2_fmeasure': tensor(0.),
         'rouge2_precision': tensor(0.),
         'rouge2_recall': tensor(0.),
         'rougeL_fmeasure': tensor(0.5000),
         'rougeL_precision': tensor(0.5000),
         'rougeL_recall': tensor(0.5000),
         'rougeLsum_fmeasure': tensor(0.5000),
         'rougeLsum_precision': tensor(0.5000),
         'rougeLsum_recall': tensor(0.5000)}


    Raises:
        ModuleNotFoundError:
            If the python package ``nltk`` is not installed.
        ValueError:
            If any of the ``rouge_keys`` does not belong to the allowed set of keys.

    References:
        [1] ROUGE: A Package for Automatic Evaluation of Summaries by Chin-Yew Lin. https://aclanthology.org/W04-1013/
    """

    if use_stemmer:
        if not _NLTK_AVAILABLE:
            raise ModuleNotFoundError("Stemmer requires that `nltk` is installed. Use `pip install nltk`.")
        import nltk

    stemmer = nltk.stem.porter.PorterStemmer() if use_stemmer else None

    if not isinstance(rouge_keys, tuple):
        rouge_keys = tuple([rouge_keys])
    for key in rouge_keys:
        if key not in ALLOWED_ROUGE_KEYS.keys():
            raise ValueError(f"Got unknown rouge key {key}. Expected to be one of {list(ALLOWED_ROUGE_KEYS.keys())}")
    rouge_keys_values = [ALLOWED_ROUGE_KEYS[key] for key in rouge_keys]

    if isinstance(target, list) and all(isinstance(tgt, str) for tgt in target):
        target = [target] if isinstance(preds, str) else [[tgt] for tgt in target]

    if isinstance(preds, str):
        preds = [preds]

    if isinstance(target, str):
        target = [[target]]

    sentence_results: Dict[Union[int, str], List[Dict[str, Tensor]]] = _rouge_score_update(
        preds, target, rouge_keys_values, stemmer=stemmer, accumulate=accumulate
    )

    output: Dict[str, List[Tensor]] = {}
    for rouge_key in rouge_keys_values:
        for type in ["fmeasure", "precision", "recall"]:
            output[f"rouge{rouge_key}_{type}"] = []

    for rouge_key, metrics in sentence_results.items():
        for metric in metrics:
            for type, value in metric.items():
                output[f"rouge{rouge_key}_{type}"].append(value)

    return _rouge_score_compute(output)
