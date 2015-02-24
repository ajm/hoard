import json
import threading
import collections

from sys import stderr, exit
from os.path import isfile, join, abspath

from glutton.db import GluttonDB
from glutton.utils import get_log, md5, check_dir


PARAM_FILE  = 'parameters.json'
CONTIG_FILE = 'contigs.json'
BLAST_FILE  = 'blastx.json'
PAGAN_FILE  = 'pagan.json'

QUERY_ID = 'query'


def do_locking(fn) :
    def thread_safe(*args) :
        args[0].lock.acquire()
        ret = fn(*args)
        args[0].lock.release()

        return ret

    return thread_safe

class GluttonException(Exception) :
    pass

class GluttonInformation(object) :
    def __init__(self, alignments_dir, db_obj_or_filename=None, contig_files=[]) :
        self.directory = alignments_dir
        check_dir(self.directory)

        self.log = get_log()
        self.lock = threading.RLock() # a single call requires this be an RLock over a Lock

        # the alignment procedure can take a long time, so everything needs to be 
        # restartable, in addition - if we restart it then we need to be sure that 
        # the parameters used are the same, i.e.: same reference database and maybe
        # more
        #
        self.params = {}
        self.contig_query_map = {}          # file id -> contig id -> query id (file id is provided by the user, called a 'label')
        self.query_gene_map = {}            # query id -> gene id
        self.genefamily_filename_map = {}   # gene family id -> filename

        self.read_progress_files()

        if ((not db_obj_or_filename) or (not contig_files)) and (not self.params) :
            self.log.fatal("reference database and contig files must be specified if you are not continuing from a previous run!")
            exit(1)

        if isinstance(db_obj_or_filename, str) :
            self.db = GluttonDB(db_obj_or_filename) 
        elif isinstance(db_obj_or_filename, GluttonDB) :
            self.db = db_obj_or_filename
        elif db_obj_or_filename is None :
            self.db = GluttonDB(self.params['db_filename'])

        # contig files may not be specified if the command is being restarted
        # check that the files are there and that the contents is the same as
        # the last time we saw it
        try :
            self.check_params(contig_files if contig_files else self.get_contig_files())

        except IOError, ioe :
            self.log.fatal(str(ioe))
            
            if not contig_files :
                self.log.fatal("this file appears to have moved, respecify with --contigs")
            
            exit(1)    

        

    def get_db(self) :
        return self.db

    def get_contig_files(self) :
        tmp = []

        for filename in self.params['contig_files'] :
            label,species,checksum = self.params['contig_files'][filename]
            tmp.append((filename, label, species))

        return tmp

    def get_labels(self) :
        return [ self.params['contig_files'][f][0] for f in self.params['contig_files'] ]

    # files used for recording the progress are just dirty globals
    # at the moment these properties can be in place of a decent solution
    # for now...
    @property
    def parameter_filename(self) :
        global PARAM_FILE
        return join(self.directory, PARAM_FILE)
   
    @property
    def contig_filename(self) :
        global CONTIG_FILE
        return join(self.directory, CONTIG_FILE)

    @property
    def blast_filename(self) :
        global BLAST_FILE
        return join(self.directory, BLAST_FILE)

    @property
    def pagan_filename(self) :
        global PAGAN_FILE
        return join(self.directory, PAGAN_FILE)

    def flush(self) :
        self.log.info("flushing data to disk...")
        self.write_progress_files()
        self.log.info("done")

    # functions and utilities to read and write the different progress files
    #
    def _load(self, fname) :
        if isfile(fname) :
            self.log.info("found progress file %s ..." % fname)
            return json.loads(open(fname).read())

        return {}

    def _dump(self, fname, data) :
        if data :
            open(fname, 'w').write(json.dumps(data))

    def read_progress_files(self) :
        self.params                     = self._load(self.parameter_filename)
        self.contig_query_map           = self._load(self.contig_filename)
        self.query_gene_map             = self._load(self.blast_filename)
        self.genefamily_filename_map    = self._load(self.pagan_filename)

        if self.contig_query_map :
            self.log.info("read %d contig to query id mappings" % sum([ len(self.contig_query_map[label]) for label in self.contig_query_map ]))

        if self.query_gene_map :
            self.log.info("read %d blast results" % len(self.query_gene_map))

        if self.genefamily_filename_map :
            self.log.info("read %d pagan results" % len(self.genefamily_filename_map))

    @do_locking
    def write_progress_files(self) :
        self._dump(self.parameter_filename, self.params)
        self._dump(self.contig_filename,    self.contig_query_map)
        self._dump(self.blast_filename,     self.query_gene_map)
        self._dump(self.pagan_filename,     self.genefamily_filename_map)

    # related to database parameters
    #
    def get_params(self, contig_files) :
        p = {}

        p['db_species']  = self.db.species
        p['db_release']  = self.db.release
        p['db_filename'] = self.db.filename
        p['db_checksum'] = self.db.checksum

        p['contig_files'] = {}

        for filename,label,species in contig_files :
            abs_filename = abspath(filename)
            p['contig_files'][abs_filename] = [label, species, md5(abs_filename)]

        return p

    @do_locking
    def check_params(self, contig_files) :
        db_params = self.get_params(contig_files)

        if not self.params :
            self.params = db_params
            return
        
        if self._not_same_db(db_params) :
            self.log.fatal("found different reference/input files/parameters!")
            
            self.log.fatal("original:")
            self._print_params(self.params)
            
            self.log.fatal("current:")
            self._print_params(db_params)
            
            exit(1)

        # this might seem odd, but the parameters can be the same (i.e. the contents of
        # the files is the same), but the actual locations of the files can change,
        # so just set it globally
        self.params = db_params

    def _print_params(self, p) :
        self.log.fatal("%s/%d" % (p['db_species'], p['db_release']))

        for filename in p['contig_files'] :
            label, species, checksum = p['contig_files'][filename]
            self.log.fatal("\t%s label=%s species=%s md5=%s" % (filename, label, species, checksum))

    def _not_same_db(self, par) :
        def get_checksums(p) :
            return sorted([ p['db_checksum'] ] + [ p['contig_files'][f] for f in p['contig_files'] ])

        return get_checksums(self.params) != get_checksums(par)

    def _set_id_counter(self) :
        tmp = [0]
        for label in self.contig_query_map :
            for i in self.contig_query_map[label].values() :
                tmp.append(int(i[len(QUERY_ID):]))

        self.query_id_counter = 1 + max(tmp)

    # contig to query ids are only get
    @do_locking
    def get_query_from_contig(self, label, contig_id) :
        global QUERY_ID

        try :
            return self.contig_query_map[label][contig_id]

        except KeyError :
            pass
        
        # well... this makes we queasy...
        #   if there is no attribute in this class called something, then
        #   just create it and initialise it to a sensible value
        if not hasattr(self, 'query_id_counter') :
            self._set_id_counter()

        new_query_id = "%s%d" % (QUERY_ID, self.query_id_counter)
        self.query_id_counter += 1

        if label not in self.contig_query_map :
            self.contig_query_map[label] = {}

        self.contig_query_map[label][contig_id] = new_query_id

        return new_query_id

    # query id to gene id
    #   update
    @do_locking
    def update_query_gene_mapping(self, new_dict) :
        self.query_gene_map.update(new_dict)

    # genefamily id to filename or FAIL
    #   put/get/fail/in
    @do_locking
    def put_genefamily2filename(self, genefamily_id, filename='FAIL') :
        self.genefamily_filename_map[genefamily_id] = filename

    def get_genefamily2filename(self, genefamily_id) :
        return self.genefamily_filename_map[genefamily_id]

    def in_genefamily2filename(self, genefamily_id) :
        return genefamily_id in self.genefamily_filename_map

    def len_genefamily2filename(self) :
        return len(self.genefamily_filename_map)

    # aggregate actions
    #
    @do_locking
    def build_genefamily2contigs(self) :
        genefamily_contig_map = collections.defaultdict(list)

        for i in self.query_gene_map :
            if self.query_gene_map[i] == 'FAIL' :
                continue

            genefamily_contig_map[self.db.get_familyid_from_geneid(self.query_gene_map[i])].append(i)
        
        return genefamily_contig_map

    @do_locking
    def pending_queries(self) :
        tmp = []

        for label in self.contig_query_map :
            for i in self.contig_query_map[label].values() :
                if i not in self.query_gene_map :
                    tmp.append(i)

        return tmp

    @do_locking
    def num_alignments_not_done(self) :
        genefamily_contig_map = self.build_genefamily2contigs()
        not_done = 0
        failures = 0

        for i in genefamily_contig_map :
            if i not in self.genefamily_filename_map :
                not_done += 1
                continue

            if self.genefamily_filename_map[i] == 'FAIL' :
                failures += 1

        return not_done, failures

    @do_locking
    def alignments_complete(self) :
        genefamily_contig_map = self.build_genefamily2contigs()

        for i in genefamily_contig_map :
            if i not in self.genefamily_filename_map :
                return False

        return True

    # filenames are relative file paths, what if they change between
    # runs? checksum is expensive, but safe...
    def _filename_to_label_via_checksum(self, filename) :
        return self._checksum_to_label(md5(filename))

    def _checksum_to_label(self, checksum) :
        for fname in self.params['contig_files'] :
            label,species,csum = self.params['contig_files'][fname]
            if checksum == csum :
                return label

        raise Exception("file with checksum %s does not exist" % checksum)

    def label_to_species(self, label) :
        for fname in self.params['contig_files'] :
            lab,species,checksum = self.params['contig_files'][fname]
            if label == lab :
                return species

        raise Exception("file label %s does not exist" % label)

    def _filename_to_label(self, filename) :
        return self.params['contig_files'][filename][0]

    def filename_to_label(self, filename) :
        return self._filename_to_label_via_checksum(filename)

    # functions used by scaffolder
    #
    @do_locking
    def contig_used(self, contig_id, label) :
        return contig_id in self.contig_query_map[label]

    @do_locking
    def contig_assigned(self, contig_id, label) :
        qid = self.contig_query_map[label][contig_id]

        return self.query_gene_map[qid] != 'FAIL'

#    @do_locking
#    def contig_aligned(self, contig_id) :
#        qid = self.contig_query_map[contig_id]
#        gid = self.query_gene_map[qid]
#        gfid = self.db.get_genefamily_from_gene(gid)
#        
#        return self.genefamily_filename_map[gfid] != 'FAIL'

    @do_locking
    def get_contig_from_query(self, query_id) :
        # lazy reverse lookup
        if not hasattr(self, 'query_contig_map') :
            self.query_contig_map = {}

            for label in self.contig_query_map :
                cqm = self.contig_query_map[label]
                for contig_id in cqm :
                    self.query_contig_map[cqm[contig_id]] = (contig_id, label)

        if isinstance(query_id, list) :
            return [ self.query_contig_map[i] for i in query_id ]

        return self.query_contig_map[query_id]

if __name__ == '__main__' :
    
    from glutton.db import GluttonDB

    db = GluttonDB('tc23.glt')
    gi = GluttonInformation(db, 'queries_test.fasta', './alignment_test')
