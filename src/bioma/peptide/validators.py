"""Independent, narrowly scoped graph checker. Not a docking/MD validator."""
from .contracts import verify_reference


def graph_validator(expected_candidates):
    """Expected graphs must come from a reviewed profile, never generated output.

    The contract pins RDKit version. Only graph, charge, stereochemistry and
    connectedness are assessed. Conformer suitability / MD are explicitly out of scope.
    """
    def validate(root, refs, identity):
        from rdkit import Chem, rdBase
        expected_version = expected_candidates['rdkit_version']
        if rdBase.rdkitVersion != expected_version:
            raise ValueError('validator_rdkit_version_mismatch')
        passed, failed = [], []
        for candidate_id, expected in expected_candidates['candidates'].items():
            matches = [ref for ref in refs if ref['relative_path'] == expected['artifact']]
            try:
                if len(matches) != 1:
                    raise ValueError('missing_candidate_artifact')
                path = verify_reference(root, matches[0], identity)
                mols = list(Chem.SDMolSupplier(str(path), removeHs=True, sanitize=True))
                if len(mols) != 1 or mols[0] is None:
                    raise ValueError('invalid_sdf')
                mol = mols[0]
                expected_mol = Chem.MolFromSmiles(expected['smiles'])
                if expected_mol is None:
                    raise ValueError('invalid_reference_graph')
                if (Chem.MolToSmiles(mol, isomericSmiles=True) != Chem.MolToSmiles(expected_mol, isomericSmiles=True)
                        or Chem.GetFormalCharge(mol) != expected['charge'] or len(Chem.GetMolFrags(mol)) != 1):
                    raise ValueError('chemical_graph_charge_or_stereochemistry_mismatch')
                passed.append(candidate_id)
            except (ValueError, OSError) as exc:
                failed.append({'candidate_id': candidate_id, 'reason': str(exc)})
        return {'validator_version': 'peptide-graph/1.0+rdkit-' + expected_version,
                'scope': 'graph_charge_stereochemistry_connectedness_only',
                'passed_candidates': passed, 'failed_candidates': failed}
    return validate
