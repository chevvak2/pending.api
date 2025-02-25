"""
Translator Node Normalizer Service Handler
"""
import logging

import biothings_client
from biothings.utils.common import get_dotfield_value
from biothings.web.handlers import BaseAPIHandler
from tornado.web import HTTPError

logger = logging.getLogger(__name__)

BIOLINK_PREFIX_to_BioThings = {
    "NCBIGene": {"type": "gene", "field": "entrezgene"},
    "ENSEMBL": {"type": "gene", "field": "ensembl.gene"},
    "UniProtKB": {"type": "gene", "field": "uniprot.Swiss-Prot"},
    "CHEMBL.COMPOUND": {
        "type": "chem",
        "field": "chembl.molecule_chembl_id",
        "converter": lambda x: x.replace("CHEMBL.COMPOUND:", "CHEMBL"),
    },
    "PUBCHEM.COMPOUND": {"type": "chem", "field": "pubchem.cid"},
    "CHEBI": {"type": "chem", "field": "chebi.id", "keep_prefix": True},
    "MONDO": {"type": "disease", "field": "mondo.mondo"},
    "DOID": {"type": "disease", "field": "doid.doid"},
}


class TRAPIInputError(ValueError):
    pass


class InvalidCurieError(ValueError):
    pass


def list2dict(li, key):
    out = {}
    for d in li:
        k = d[key]
        if k not in out:
            out[k] = [d]
        else:
            out[k].append(d)
    return out


class Normalizer:
    normalizer_clients = {
        "gene": {
            "client": biothings_client.get_client("gene"),
            "fields": ["name", "symbol", "summary", "type_of_gene", "MIM"],
            "scopes": ["entrezgene", "ensemblgene", "uniprot", "accession", "retired"],
        },
        "chem": {
            "client": biothings_client.get_client("chem"),
            "fields": [
                "drugbank.id",
                "chebi.id",
                "chebi.iupac",
                "chebi.relationship",
                "chembl.smiles",
                "chembl.first_approval",
                "chembl.first_in_class",
                "chembl.unii",
                "chembl.drug_indications",
                "chembl.drug_mechanisms",
                "pubchem.molecular_weight",
                "pubchem.molecular_formula",
                "drugcentral.approval",
            ],
            "scopes": ["chebi.id", "chembl.molecule_chembl_id", "pubchem.cid", "drugbank.id", "unii.unii"],
        },
        "disease": {
            "client": biothings_client.get_client("disease"),
            "fields": ["mondo.mondo", "mondo.label", "mondo.definition", "umls.umls"],
            "scopes": ["mondo.mondo", "doid.doid", "umls.umls"],
        },
    }

    def parse_curie(self, curie, return_type=True, return_id=True):
        """return a both type and if (as a tuple) or either based on the input curie"""
        if ":" not in curie:
            raise InvalidCurieError(f"Invalid input curie id: {curie}")
        _prefix, _id = curie.split(":", 1)
        _type = BIOLINK_PREFIX_to_BioThings.get(_prefix, {}).get("type", None)
        if return_id:
            if not _type or BIOLINK_PREFIX_to_BioThings[_prefix].get("keep_prefix", False):
                _id = curie
            cvtr = BIOLINK_PREFIX_to_BioThings.get(_prefix, {}).get("converter", None)
            if cvtr:
                _id = cvtr(curie)
        if return_type and return_id:
            return _type, _id
        elif return_type:
            return _type
        elif return_id:
            return _id

    def query_biothings(self, node_type, query_list, fields=None):
        """Query biothings client based on node_type for a list of ids"""
        client = self.normalizer_clients[node_type]["client"]
        fields = fields or self.normalizer_clients[node_type]["fields"]
        scopes = self.normalizer_clients[node_type]["scopes"]
        logger.info("Querying annotations for %s %ss...", len(query_list), node_type)
        res = client.querymany(query_list, scopes=scopes, fields=fields)
        logger.info("Done. %s annotation objects returned.", len(res))
        res = list2dict(res, "query")
        return res

    def annotate_curie(self, curie, raw=False, fields=None):
        """Annotate a single curie id"""
        node_type, _id = self.parse_curie(curie)
        res = self.query_biothings(node_type, [_id], fields=fields)
        if not raw:
            res = [self.transform(r) for r in res[_id]]
        return {curie: res}

    def transform(self, res):
        """perform any transformation on the annotation object, but in-place also returned object"""
        res.pop("query", None)
        res.pop("_score", None)
        return res

    def annotate_trapi(self, trapi_input, append=False, raw=False, fields=None):
        """Annotate a TRAPI input message with node normalizer annotations"""
        try:
            node_d = get_dotfield_value("message.knowledge_graph.nodes", trapi_input)
            assert isinstance(node_d, dict)
        except (KeyError, ValueError, AssertionError):
            raise TRAPIInputError("Invalid input format")

        node_list_by_type = {}
        for node_id in node_d:
            node_type = self.parse_curie(node_id, return_type=True, return_id=False)
            if not node_type:
                logger.info("%s - %s", node_type, node_id)
            if node_type:
                if node_type not in node_list_by_type:
                    node_list_by_type[node_type] = [node_id]
                else:
                    node_list_by_type[node_type].append(node_id)
        for node_type in node_list_by_type:
            if node_type not in self.normalizer_clients or not node_list_by_type[node_type]:
                # skip for now
                continue
            # this is the list of original node ids like NCBIGene:1017, should be a unique list
            node_list = node_list_by_type[node_type]
            # this is the list of query ids like 1017
            query_list = [
                self.parse_curie(_id, return_type=False, return_id=True) for _id in node_list_by_type[node_type]
            ]
            # query_id to original id mapping
            node_id_d = dict(zip(query_list, node_list))
            res_by_id = self.query_biothings(node_type, query_list, fields=fields)
            for node_id in res_by_id:
                orig_node_id = node_id_d[node_id]
                res = res_by_id[node_id]
                if not raw:
                    if isinstance(res, list):
                        # TODO: handle multiple results here
                        res = [self.transform(r) for r in res]
                    else:
                        res = self.transform(res)
                res = {
                    "attribute_type_id": "biothings_annnotations",
                    "value": res,
                }
                if append:
                    # append annotations to existing "attributes" field
                    node_d[orig_node_id]["attributes"].append(res)
                else:
                    # return annotations only
                    node_d[orig_node_id]["attributes"] = [res]

        return node_d


class NormalizerHandler(BaseAPIHandler):
    name = "normalizer"
    kwargs = {
        "*": {
            "raw": {"type": bool, "default": False},
            "fields": {"type": str, "default": None},
        },
        "POST": {
            "append": {"type": bool, "default": False},
        },
    }

    async def get(self, *args, **kwargs):
        normalizer = Normalizer()
        curie = args[0] if args else None
        if curie:
            annotated_node = normalizer.annotate_curie(curie, raw=self.args.raw, fields=self.args.fields)
            self.finish(annotated_node)
        else:
            raise HTTPError(404, reason="missing required input curie id")

    async def post(self, *args, **kwargs):
        normalizer = Normalizer()
        try:
            annotated_node_d = normalizer.annotate_trapi(
                self.args_json,
                append=self.args.append,
                raw=self.args.raw,
                fields=self.args.fields,
            )
        except TRAPIInputError as e:
            raise HTTPError(400, str(e))
        self.finish(annotated_node_d)
