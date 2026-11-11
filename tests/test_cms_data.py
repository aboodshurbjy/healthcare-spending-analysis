"""Tests for the cleaning and join logic in src/cms_data.py.

These use small hand-built frames shaped like the CMS files, so they run
without downloading anything.
"""

import numpy as np
import pandas as pd
import pytest

import cms_data


def make_hospital_raw():
    return pd.DataFrame(
        {
            "Facility ID": ["010001", "10005 ", "330101", "450002"],
            "Facility Name": ["A", "B", "C", "D"],
            "City/Town": ["X", "Y", "Z", "W"],
            "State": ["AL", "AL", "NY", "TX"],
            "ZIP Code": ["36301", "35957", "10001", "75001"],
            "County/Parish": ["H", "M", "N", "D"],
            "Hospital Type": ["Acute Care Hospitals"] * 4,
            "Hospital Ownership": [
                "Voluntary non-profit - Private",
                "Proprietary",
                "Government - Local",
                "Veterans Health Administration",
            ],
            "Hospital overall rating": ["4", "Not Available", "1", "5"],
            "Unused Column": ["a", "b", "c", "d"],
        }
    )


def make_readmit_raw():
    rows = [
        # facility, measure, err, predicted, expected
        ("010001", "READM-30-AMI-HRRP", "0.9000", "13.0", "14.4"),
        ("010001", "READM-30-HF-HRRP", "1.1000", "22.0", "20.0"),
        ("010001", "READM-30-HIP-KNEE-HRRP", None, None, None),
        ("010005", "READM-30-PN-HRRP", "1.0500", "16.8", "16.0"),
        ("010005", "READM-30-CABG-HRRP", "Too Few to Report", None, None),
        # hospital with no reported ratios at all
        ("330101", "READM-30-COPD-HRRP", None, None, None),
        # hospital that is not in the hospital info file
        ("999999", "READM-30-COPD-HRRP", "1.2000", "20.0", "16.7"),
        # a measure that is not one of the six HRRP conditions
        ("010001", "SOME-OTHER-MEASURE", "1.5000", "10.0", "10.0"),
    ]
    return pd.DataFrame(
        rows,
        columns=[
            "Facility ID",
            "Measure Name",
            "Excess Readmission Ratio",
            "Predicted Readmission Rate",
            "Expected Readmission Rate",
        ],
    )


def make_spending_raw():
    return pd.DataFrame(
        {
            "Facility ID": ["010001", "010005", "330101", "450002"],
            "Measure ID": ["MSPB-1"] * 4,
            "Score": ["0.99", "Not Available", "1.10", "1.02"],
        }
    )


# -- small helpers ------------------------------------------------------------

def test_to_numeric_turns_cms_placeholders_into_nan():
    result = cms_data.to_numeric(
        pd.Series(["1.05", "Not Available", "Too Few to Report", None])
    )
    assert result.iloc[0] == pytest.approx(1.05)
    assert result.iloc[1:].isna().all()


def test_normalize_facility_id_strips_and_pads():
    result = cms_data.normalize_facility_id(pd.Series([" 10005", "010001", "50"]))
    assert result.tolist() == ["010005", "010001", "000050"]


@pytest.mark.parametrize(
    "measure, expected",
    [
        ("READM-30-AMI-HRRP", "AMI"),
        ("READM-30-HF-HRRP", "HF"),
        ("READM-30-PN-HRRP", "PN"),
        ("READM-30-COPD-HRRP", "COPD"),
        ("READM-30-CABG-HRRP", "CABG"),
        ("READM-30-HIP-KNEE-HRRP", "HIP/KNEE"),
        ("readm-30-hf-hrrp ", "HF"),
        ("READM-30-XYZ-HRRP", None),
        ("MSPB-1", None),
        (None, None),
    ],
)
def test_classify_condition(measure, expected):
    assert cms_data.classify_condition(measure) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Voluntary non-profit - Church", "Voluntary Non-Profit"),
        ("Proprietary", "Proprietary (For-Profit)"),
        ("Physician", "Proprietary (For-Profit)"),
        ("Government - State", "Government"),
        ("Tribal", "Government"),
        ("Department of Defense", "Government"),
        ("Veterans Health Administration", "Other"),
        (None, "Other"),
    ],
)
def test_simplify_ownership(raw, expected):
    assert cms_data.simplify_ownership(raw) == expected


# -- cleaning -----------------------------------------------------------------

def test_clean_hospital_info_columns_and_rating():
    clean = cms_data.clean_hospital_info(make_hospital_raw())
    assert "Unused Column" not in clean.columns
    assert clean["facility_id"].tolist() == ["010001", "010005", "330101", "450002"]
    assert clean["rating"].tolist()[0] == 4.0
    assert np.isnan(clean["rating"].tolist()[1])
    assert clean["ownership_simple"].tolist() == [
        "Voluntary Non-Profit",
        "Proprietary (For-Profit)",
        "Government",
        "Other",
    ]


def test_clean_hospital_info_rejects_missing_columns():
    raw = make_hospital_raw().drop(columns=["Hospital Ownership"])
    with pytest.raises(KeyError, match="Hospital Ownership"):
        cms_data.clean_hospital_info(raw)


def test_clean_hospital_info_rejects_duplicate_ids():
    raw = pd.concat([make_hospital_raw(), make_hospital_raw().iloc[[0]]])
    with pytest.raises(ValueError, match="duplicate"):
        cms_data.clean_hospital_info(raw)


def test_clean_readmissions_long_drops_unreported_and_unknown_rows():
    long = cms_data.clean_readmissions_long(make_readmit_raw())
    # 4 rows have both a numeric ratio and a recognized HRRP measure
    assert len(long) == 4
    assert set(long["condition"]) == {"AMI", "HF", "PN", "COPD"}
    assert long["err"].notna().all()


def test_clean_readmissions_pivots_and_averages_reported_conditions():
    wide = cms_data.clean_readmissions(make_readmit_raw()).set_index("facility_id")
    # 330101 has no reported ratio, so it is absent
    assert sorted(wide.index) == ["010001", "010005", "999999"]
    assert list(wide.columns) == cms_data.ERR_COLUMNS + ["avg_err", "n_conditions"]
    row = wide.loc["010001"]
    assert row["ERR_AMI"] == pytest.approx(0.90)
    assert row["ERR_HF"] == pytest.approx(1.10)
    assert np.isnan(row["ERR_HIP/KNEE"])
    # mean of the two reported conditions only, not of six
    assert row["avg_err"] == pytest.approx(1.00)
    assert row["n_conditions"] == 2
    assert wide.loc["010005", "avg_err"] == pytest.approx(1.05)


def test_clean_spending_keeps_unavailable_scores_as_nan():
    clean = cms_data.clean_spending(make_spending_raw()).set_index("facility_id")
    assert len(clean) == 4
    assert clean.loc["010001", "spending_ratio"] == pytest.approx(0.99)
    assert np.isnan(clean.loc["010005", "spending_ratio"])


def test_clean_spending_ignores_other_measures():
    raw = make_spending_raw()
    extra = pd.DataFrame(
        {"Facility ID": ["010001"], "Measure ID": ["OTHER-1"], "Score": ["5.00"]}
    )
    clean = cms_data.clean_spending(pd.concat([raw, extra])).set_index("facility_id")
    assert clean.loc["010001", "spending_ratio"] == pytest.approx(0.99)


# -- join ---------------------------------------------------------------------

def merged_fixture():
    return cms_data.merge_datasets(
        cms_data.clean_hospital_info(make_hospital_raw()),
        cms_data.clean_readmissions(make_readmit_raw()),
        cms_data.clean_spending(make_spending_raw()),
    )


def test_merge_is_inner_join_on_facility_id():
    merged = merged_fixture()
    # 330101 has no readmission ratio, 450002 has no readmission rows,
    # 999999 is not in hospital info. Only two hospitals are in all three.
    assert merged["facility_id"].tolist() == ["010001", "010005"]
    assert merged["facility_id"].is_unique


def test_merge_matches_ids_that_differ_only_by_padding():
    # "10005 " in the hospital file must match "010005" in the other two
    merged = merged_fixture().set_index("facility_id")
    assert merged.loc["010005", "facility_name"] == "B"
    assert merged.loc["010005", "avg_err"] == pytest.approx(1.05)


def test_merge_keeps_row_with_missing_spending():
    merged = merged_fixture().set_index("facility_id")
    assert np.isnan(merged.loc["010005", "spending_ratio"])
    assert merged.loc["010001", "spending_ratio"] == pytest.approx(0.99)


def test_merge_rejects_duplicate_keys():
    hospital = cms_data.clean_hospital_info(make_hospital_raw())
    readmit = cms_data.clean_readmissions(make_readmit_raw())
    spending = cms_data.clean_spending(make_spending_raw())
    duplicated = pd.concat([spending, spending.iloc[[0]]])
    with pytest.raises(pd.errors.MergeError):
        cms_data.merge_datasets(hospital, readmit, duplicated)
