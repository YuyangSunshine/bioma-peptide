import pytest
from bioma.peptide.feature_validation import uncertain_feature_coordinates, validate_feature_delivery
from bioma.harness.delivery_review import ReviewIssue, _verified_violation

SOURCE = '''ID   TEST_HUMAN              Reviewed;         4 AA.
AC   P00001;
FT   CHAIN           2..?
FT                   /note="Processed chain"
SQ   SEQUENCE   4 AA;  100 MW;  0000000000000000 CRC64;
     AAAA
//
'''
TABLE = 'type,start,end,annotation\nCHAIN,2,2,Processed chain\n'


def test_uncertain_endpoint_cannot_become_exact():
    assert uncertain_feature_coordinates(SOURCE, TABLE)[0] is True
    assert uncertain_feature_coordinates(SOURCE, TABLE.replace('2,2,', '2,?,'))[0] is False
    assert uncertain_feature_coordinates(SOURCE.replace('2..?', '<2..4'), TABLE)[0] is True
    assert uncertain_feature_coordinates(SOURCE.replace('2..?', '2..4'), TABLE)[0] is None


def test_unmatched_or_unparseable_material_never_confirms_violation():
    assert uncertain_feature_coordinates(SOURCE, TABLE.replace('Processed chain', 'Different chain'))[0] is None
    assert uncertain_feature_coordinates('not Swiss-Prot', TABLE)[0] is None
    assert uncertain_feature_coordinates(SOURCE, 'unsupported,columns\n1,2')[0] is None
    assert uncertain_feature_coordinates(SOURCE, 'type,start,end,annotation,uncertain\nCHAIN,2,2,Processed chain,true')[0] is None
    assert uncertain_feature_coordinates(SOURCE * 10000, TABLE)[0] is None


def test_review_requires_original_source_and_independently_parses_full_documents():
    issue = ReviewIssue(location='/artifacts/regions.csv', message='Non-exact endpoint collapsed',
                        disposition='confirmed', category='numeric', check='uniprot_feature_location',
                        basis_kind='source', basis={'pointer':'/original_inputs/source.txt','quote':'2..?'},
                        observation={'pointer':'/artifacts/regions.csv','quote':'CHAIN,2,2,Processed chain'})
    payload={'original_inputs':{'source.txt':SOURCE}, 'artifacts':{'regions.csv':TABLE}}
    assert _verified_violation(issue,payload)[0] is True
    issue.basis_kind='requirement'
    assert _verified_violation(issue,payload)[0] is None


def test_feature_delivery_rejects_without_a_model_finding():
    with pytest.raises(ValueError, match='non-exact source end'):
        validate_feature_delivery({'source.txt':SOURCE}, {'regions.csv':TABLE})
    checks=validate_feature_delivery({'source.txt':SOURCE}, {'regions.csv':TABLE.replace('2,2,','2,?,')})
    assert checks[0]['passed'] is True
    assert validate_feature_delivery({'one.txt':SOURCE, 'two.txt':SOURCE}, {'regions.csv':TABLE}) == []
