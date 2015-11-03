"""
This file is concerned with providing a simple interface for data stored in
Elasticsearch.  The class(es) defined here are fed into the preprocessing step.
"""

from abc import ABCMeta, abstractmethod, abstractproperty
import logging
import time

from six import with_metaclass

from gensim.corpora.dictionary import Dictionary

from topik.fileio.persistence import Persistor
from topik.tokenizers import tokenizer_methods
from topik.fileio.tokenized_corpus import TokenizedCorpus


registered_outputs = {}

def register_output(cls):
    global registered_outputs
    if cls.class_key() not in registered_outputs:
        registered_outputs[cls.class_key()] = cls
    return cls


def _get_hash_identifier(input_data, field_to_hash):
    return hash(input_data[field_to_hash])


def _get_parameters_string(**kwargs):
    """Used to create identifiers for output"""
    id = ''.join('{}={}_'.format(key, val) for key, val in sorted(kwargs.items()))
    return id[:-1]


class CorpusInterface(with_metaclass(ABCMeta)):
    def __init__(self):
        super(CorpusInterface, self).__init__()
        self.persistor = Persistor()

    @classmethod
    @abstractmethod
    def class_key(cls):
        """Implement this method to return the string ID with which to store your class."""
        raise NotImplementedError

    @abstractmethod
    def __iter__(self):
        """This is expected to iterate over your data, returning tuples of (doc_id, <selected field>)"""
        raise NotImplementedError

    @abstractmethod
    def __len__(self):
        raise NotImplementedError

    @abstractmethod
    def get_generator_without_id(self, field=None):
        """Returns a generator that yields field content without doc_id associate"""
        raise NotImplementedError

    @abstractmethod
    def get_date_filtered_data(self, start, end, field):
        raise NotImplementedError

    @abstractproperty
    def filter_string(self):
        raise NotImplementedError

    def save(self, filename, saved_data=None):
        """Persist this object to disk somehow.

        You can save your data in any number of files in any format, but at a minimum, you need one json file that
        describes enough to bootstrap the loading prcess.  Namely, you must have a key called 'class' so that upon
        loading the output, the correct class can be instantiated and used to load any other data.  You don't have
        to implement anything for saved_data, but it is stored as a key next to 'class'.

        """
        self.persistor.store_corpus({"class": self.__class__.class_key(), "saved_data": saved_data})
        self.persistor.persist_data(filename)

    def synchronize(self, max_wait, field):
        """By default, operations are synchronous and no additional wait is
        necessary.  Data sources that are asynchronous (ElasticSearch) may
        use this function to wait for "eventual consistency" """
        pass

@register_output
class ElasticSearchCorpus(CorpusInterface):
    def __init__(self, source, index, content_field, doc_type='continuum', query=None, iterable=None,
                 filter_expression="", **kwargs):
        from elasticsearch import Elasticsearch
        super(ElasticSearchCorpus, self).__init__()
        self.hosts = source
        self.instance = Elasticsearch(hosts=source, **kwargs)
        self.index = index
        self.content_field = content_field
        self.doc_type = doc_type
        self.query = query
        if iterable:
            self.import_from_iterable(iterable, content_field)
        self.filter_expression = filter_expression

    @classmethod
    def class_key(cls):
        return "elastic"

    @property
    def filter_string(self):
        return self.filter_expression

    def __iter__(self):
        from elasticsearch import helpers
        results = helpers.scan(self.instance, index=self.index,
                               query=self.query, doc_type=self.doc_type)
        for result in results:
            yield result["_id"], result['_source'][self.content_field]

    def __len__(self):
        return self.instance.count(index=self.index, doc_type=self.doc_type)["count"]

    def get_generator_without_id(self, field=None):
        if not field:
            field = self.content_field
        for (_, result) in ElasticSearchCorpus(self.hosts, self.index, field, self.doc_type, self.query):
            yield result

    def append_to_record(self, record_id, field_name, field_value):
        self.instance.update(index=self.index, id=record_id, doc_type=self.doc_type,
                             body={"doc": {field_name: field_value}})

    def get_field(self, field=None):
        """Get a different field to iterate over, keeping all other
           connection details."""
        if not field:
            field = self.content_field
        return ElasticSearchCorpus(self.hosts, self.index, field, self.doc_type, self.query)

    def import_from_iterable(self, iterable, content_field="text", batch_size=500):
        """Load data into Elasticsearch from iterable.

        iterable: generally a list of dicts, but possibly a list of strings
            This is your data.  Your dictionary structure defines the schema
            of the elasticsearch index.
        content_field: string identifier of field to hash for content ID.  For
            list of dicts, a valid key value in the dictionary is required. For
            list of strings, a dictionary with one key, "text" is created and
            used.
        """
        from elasticsearch import helpers
        batch = []
        for item in iterable:
            if isinstance(item, basestring):
                item = {content_field: item}
            id = _get_hash_identifier(item, content_field)
            action = {'_op_type': 'update',
                      '_index': self.index,
                      '_type': self.doc_type,
                      '_id': id,
                      'doc': item,
                      'doc_as_upsert': "true",
                      }
            batch.append(action)
            if len(batch) >= batch_size:
                helpers.bulk(client=self.instance, actions=batch, index=self.index)
                batch = []
        if batch:
            helpers.bulk(client=self.instance, actions=batch, index=self.index)
        self.instance.indices.refresh(self.index)

    def convert_date_field_and_reindex(self, field):
        index = self.index
        if self.instance.indices.get_field_mapping(field=field,
                                           index=index,
                                           doc_type=self.doc_type) != 'date':
            index = self.index+"_{}_alias_date".format(field)
            if not self.instance.indices.exists(index) or self.instance.indices.get_field_mapping(field=field,
                                           index=index,
                                           doc_type=self.doc_type) != 'date':
                mapping = self.instance.indices.get_mapping(index=self.index,
                                                            doc_type=self.doc_type)
                mapping[self.index]["mappings"][self.doc_type]["properties"][field] = {"type": "date"}
                self.instance.indices.put_alias(index=self.index,
                                                name=index,
                                                body=mapping)
                self.instance.indices.refresh(index)
                while self.instance.count(index=self.index) != self.instance.count(index=index):
                    logging.info("Waiting for date indexed data to be indexed...")
                    time.sleep(1)
        return index

    # TODO: validate input data to ensure that it has valid year data
    def get_date_filtered_data(self, start, end, filter_field="date"):
        converted_index = self.convert_date_field_and_reindex(field=filter_field)
        return ElasticSearchCorpus(self.hosts, converted_index, self.content_field, self.doc_type,
                                   query={"query": {"filtered": {"filter": {"range": {filter_field: {"gte": start,
                                                                                              "lte": end}}}}}},
                                   filter_expression=self.filter_expression + "_date_{}_{}".format(start, end))

    def save(self, filename, saved_data=None):
        if saved_data is None:
            saved_data = {"source": self.hosts, "index": self.index, "content_field": self.content_field,
                          "doc_type": self.doc_type, "query": self.query}
        return super(ElasticSearchCorpus, self).save(filename, saved_data)

    def synchronize(self, max_wait, field):
        # TODO: change this to a more general condition for wider use, including read_input
        # could just pass in a string condition and then 'while not eval(condition)'
        count_not_yet_updated = -1
        while count_not_yet_updated != 0:
            count_not_yet_updated = self.instance.count(index=self.index,
                                             doc_type=self.doc_type,
                                             body={"query": {
                                                        "constant_score" : {
                                                            "filter" : {
                                                                "missing" : {
                                                                    "field" : field}}}}})['count']
            logging.debug("Count not yet updated: {}".format(count_not_yet_updated))
            time.sleep(0.01)
        pass

@register_output
class DictionaryCorpus(CorpusInterface):
    def __init__(self, content_field, iterable=None, from_existing_corpus=False,
                 active_field=None, content_filter=None):
        super(DictionaryCorpus, self).__init__()
        self.content_field = content_field
        if active_field is None:
            self.active_field = content_field
        else:
            self.active_field = active_field
        self._documents = {}
        if from_existing_corpus:
            self._documents = iterable
        elif iterable:
            self.import_from_iterable(iterable, content_field)

        self.idx = 0

        self.content_filter = content_filter

    @classmethod
    def class_key(cls):
        return "dictionary"

    def __iter__(self):
        for doc_id, doc in self._documents.items():
            if self.content_filter:
                if eval(self.content_filter["expression"].format(doc["_source"][self.content_filter["field"]])):
                    yield doc_id, doc["_source"][self.active_field]
            else:
                yield doc_id, doc["_source"][self.active_field]

    def __len__(self):
        return len(self._documents)

    @property
    def filter_string(self):
        return self.content_filter["expression"].format(self.content_filter["field"]) if self.content_filter else ""

    def append_to_record(self, record_id, field_name, field_value):
        if record_id in self._documents.keys():
            self._documents[record_id]["_source"][field_name] = field_value
        else:
            raise ValueError("No record with id '{}' was found.".format(record_id))

    def term_topic_matrix(self):
        self._term_topic_matrix={}

    def get_field(self, new_active_field=None):
        """Get a different field to iterate over, keeping all other details."""
        if not new_active_field:
            new_active_field = self.content_field
        return DictionaryCorpus(active_field=new_active_field,
                                iterable=self._documents,
                                from_existing_corpus=True,
                                content_field=self.content_field)

    def get_generator_without_id(self, field=None):
        if not field:
            field = self.active_field
        for doc in self._documents.values():
            yield doc["_source"][field]

    def import_from_iterable(self, iterable, content_field):
        """
        iterable: generally a list of dicts, but possibly a list of strings
            This is your data.  Your dictionary structure defines the schema
            of the elasticsearch index.
        """

        for item in iterable:
            if isinstance(item, basestring):
                item = {content_field: item}
            id = _get_hash_identifier(item, content_field)
            self._documents[id] = {"_source": item}

    # TODO: generalize for datetimes
    # TODO: validate input data to ensure that it has valid year data
    def get_date_filtered_data(self, start, end, filter_field="year"):
        return DictionaryCorpus(content_field=self.content_field,
                                iterable=self._documents,
                                from_existing_corpus=True,
                                content_filter={"field": filter_field, "expression": "{}<=int({})<={}".format(start, "{}", end)})

    def save(self, filename, saved_data=None):
        if saved_data is None:
            saved_data = {"active_field": self.active_field, "content_field": self.content_field,
                          "iterable": self._documents,
                          "from_existing_corpus": True}
        return super(DictionaryCorpus, self).save(filename, saved_data)

def load_persisted_corpus(filename):
    corpus_dict = Persistor(filename).get_corpus_dict()
    return registered_outputs[corpus_dict['class']](**corpus_dict["saved_data"])
