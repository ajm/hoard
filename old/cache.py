import sys
import commands
import re
import os
import shutil
import hashlib
import string
import tempfile
import glob
import time

import Queue
import threading

from cogent.parse.fasta import MinimalFastaParser
from cogent.db.ensembl import Species, Genome, Compara, HostAccount
from cogent.db.ensembl.database import Database

from lib.progress import Progress
from lib.tools import Prank, PrankError
from lib.datatypes import Sequence
from lib.filetypes import FastqFile
from lib.manifest import Manifest, ManifestError

from lib.base import Base

class EnsemblDbInfo(object) :
    def __init__(self, db_name, low_release, high_release) :
        self.db_name = db_name
        self.latin_name = self._db2latin(self.db_name)
        self.common_name = self._latin2common(self.latin_name)
        self.low_release = low_release
        self.high_release = high_release
        self.release_str = "%d-%d" % (self.low_release, self.high_release)

        #print "%s %s %s" % (self.db_name, self.latin_name, self.common_name)

    def _db2latin(self, db_name) :
        tmp = Species.getSpeciesName(db_name)
        
        if tmp != 'None' :
            return tmp

        return db_name.capitalize().replace('_', ' ')

    def _latin2common(self, latin_name) :
        try :
            return Species.getCommonName(latin_name)
        except :
            pass

        tokens = latin_name.lower().split()

        if len(tokens) == 2 :
            return tokens[0][0].capitalize() + "." + tokens[1]

        raise Exception("Bad latin name: %s" % latin_name)

    def table_str(self, latin_width, common_width, release_width) :
        return self.latin_name.rjust(latin_width) + \
               self.common_name.rjust(common_width) + \
               self.release_str.rjust(release_width)
    
    def __str__(self) :
        return "%s %s %s %s" % (self.latin_name, self.common_name, self.release_str, self.db_name)

class EnsemblInfo(object) :
    def __init__(self, options) :
        self.db_host = options['db-host']
        self.db_port = options['db-port']
        self.db_user = options['db-user']
        self.db_pass = options['db-pass']

        self.verbose = options['verbose']

        self.databases = self._get_databases()

    def _convert_to_range(self, releases) :
        releases.sort()
        return "%d-%d" % (releases[0], releases[-1])

    def _get_databases(self) :
        passwd = "" if self.db_pass == "" else "-p %s" % self.db_pass
        showdb = "mysql -h %s -u %s -P %d %s -B -e 'SHOW DATABASES;'" % (self.db_host, self.db_user, self.db_port, passwd)

        stat,output = commands.getstatusoutput(showdb)

        if stat != 0 :
            print >> sys.stderr, "Error: could not run \"%s\"" % showdb
            sys.exit(-1)

        dbpat = re.compile("^(.*)_core_(\d+_)?(\d+)_.+")
        db2rel = {}

        for dbdesc in output.split('\n') :
            if "core" in dbdesc : 
                m = dbpat.match(dbdesc)
                if (m != None) and (len(m.groups()) == 3) :
                    dbname,chaff,dbrel = m.groups()
                    if dbname not in db2rel :
                        db2rel[dbname] = []
                    if chaff is None :
                        db2rel[dbname].append(int(dbrel))
                    else :
                        # in the case of the ensembl-metazoa species
                        db2rel[dbname].append(int(chaff[:-1]))

        databases = {}

        for dbname in db2rel :
            try :
                databases[dbname] = EnsemblDbInfo(dbname, min(db2rel[dbname]), max(db2rel[dbname]))
                
                # add to pycogent as well
                if Species.getSpeciesName(databases[dbname].latin_name) == 'None' :
                    if self.verbose :
                        print >> sys.stderr, "Info: adding '%s' to pycogent" % databases[dbname].latin_name
                    Species.amendSpecies(databases[dbname].latin_name, databases[dbname].common_name)
                    
                    #print >> sys.stderr, "\t" + Species.getCommonName(databases[dbname].latin_name)
                    #print >> sys.stderr, "\t" + Species.getEnsemblDbPrefix(databases[dbname].latin_name)
            except :
                if self.verbose :
                    print >> sys.stderr, "Info: rejected '%s'" % dbname

        return databases

    def get_latest_release(self, species) :
        try :
            return self.databases.get(Species.getEnsemblDbPrefix(species)).high_release
        except :
            return -1

    def _calc_rjust(self, title, variable) :
        return len(sorted([title] + map(lambda x: getattr(x, variable), self.databases.values()), key=len, reverse=True)[0]) + 2

    def print_species_table(self) :
        l_len = self._calc_rjust("Name", "latin_name")
        c_len = self._calc_rjust("Common name", "common_name")
        r_len = self._calc_rjust("Releases", "release_str")

        print "Name".rjust(l_len) + "Common Name".rjust(c_len) + "Releases".rjust(r_len)
        print "-" * (l_len + c_len + r_len)

        for name in Species.getSpeciesNames() :
            try :
                print self.databases[Species.getEnsemblDbPrefix(name)].table_str(l_len, c_len, r_len)
            except KeyError, ke :
                pass

    def is_valid_species(self, species) :
        try :
            return self.databases.has_key(Species.getEnsemblDbPrefix(species))
        except :
            return False

    def is_valid_release(self, species, release) :
        if not self.is_valid_species(species) :
            return False

        tmp = self.databases[Species.getEnsemblDbPrefix(species)]

        return (release >= tmp.low_release) and (release <= tmp.high_release)

class EnsemblDownloader(Base) :
    def __init__(self, opt) :
        super(EnsemblDownloader, self).__init__(opt)
        
        self.species = opt['species']
        self.release = opt['release']
        self.account = HostAccount(
                            opt['db-host'], 
                            opt['db-user'], 
                            opt['db-pass'], 
                            port=opt['db-port']
                         )
        self.genes = set()

    def set_already_downloaded(self, genes) :
        self.genes.update(genes)

    def __iter__(self) :
        return self.genefamilies()

    def genefamilies(self) :
        genome = Genome(self.species, Release=self.release, account=self.account)
        compara = Compara([self.species], Release=self.release, account=self.account)

        self.warn("current version only works with species in ensembl and ensembl-metazoa")

        # DON'T TRY THIS AT HOME!
        #
        # what happens is it searches for compara databases, but unfortunately finds more than one
        # in this situation pycogent just connects to the first one, which is always compara_bacteria
        # so one solution is to dig through all the compara objects internals to provide a connection
        # to the correct database ... obviously not the best solution, but at 6 lines of code definitely 
        # the shortest ;-P
        #
        if self.database == 'ensembl-genomes' :
            from cogent.db.ensembl.host import DbConnection
            from cogent.db.ensembl.name import EnsemblDbName
            import sqlalchemy

            new_db_name = EnsemblDbName(compara.ComparaDb.db_name.Name.replace('bacteria', 'metazoa'))
            compara.ComparaDb._db = DbConnection(account=self.account, db_name=new_db_name)
            compara.ComparaDb._meta = sqlalchemy.MetaData(compara.ComparaDb._db)
        # end of DON'T TRY THIS AT HOME!

        for gene in genome.getGenesMatching(BioType='protein_coding') :
            stableid = gene.StableId.lower()

            # ignore genes that have already been seen as members of
            # gene families
            if stableid in self.genes :
                continue

            self.genes.add(stableid)

            # get cds sequences of any paralogs
            paralogs = compara.getRelatedGenes(StableId=stableid, Relationship='within_species_paralog')

            paralog_seqs = {}
            if paralogs is None :
                paralog_seqs[stableid] = gene.getLongestCdsTranscript().Cds
            else :
                for paralog in paralogs.Members :
                    paralog_id = paralog.StableId.lower()
                    self.genes.add(paralog_id)
                    paralog_seqs[paralog_id] = paralog.getLongestCdsTranscript().Cds

            yield paralog_seqs


class TranscriptCache(object) :
    file_prefix = 'paralog_'
    queue_timeout = 1

    def __init__(self, options) :
        self.stop = False
        self.download_complete = False
        self.alignments_complete = False
        self.workingdir = options['workingdir']
        self.tmpdir = options['tmpdir']
        self.species = options['species']
        self.species2 = options['species2']
        self.release = options['release']
        self.account = HostAccount(options['db-host'], options['db-user'], options['db-pass'], port=options['db-port'])
        self.basedir = os.path.join(self.workingdir, str(self.release), self.species)
        self.restart = options['restart']
        self.database = options['database'] # this is necessary for a hack used later

        self.prank = options['prank']

        self.species = Species.getCommonName(self.species)
        self.basedir = os.path.join(self.workingdir, str(self.release), self.species)

        self._check_directory(self.tmpdir, create=True)
        self._check_directory(self.basedir, create=True)

        self.genes = set()

        # alignments are handled in multiple threads
        self.prank_threads = options['threads'] if options['threads'] > 0 else 1
        self.alignment_queue = Queue.Queue()

        self.alignment_threads = []

        for i in range(self.prank_threads) :
            t = threading.Thread(target=self._consume_alignment_queue)
            t.daemon = True
            self.alignment_threads.append(t)

        # there is a separate thread to write the manifest and files
        # interactions with the manifest are not thread-safe (and the workload is miniscule anyway)
        self.manifest_queue = Queue.Queue()
        self.manifest_thread = threading.Thread(target=self._consume_manifest_queue)
        self.manifest_thread.daemon = True

        if self.restart :
            self._reset_cache()
        
        try :
            self.manifest = Manifest(options, self.basedir, self.file_prefix)
            self.genes = self.manifest.get_genes()
            
            for fname in self.manifest.get_realignments() :
                self._add_to_alignment_queue(os.path.join(self.basedir, fname))

        except ManifestError, me :
            print >> sys.stderr, str(me)
            sys.exit(-1)

        for t in self.alignment_threads :
            t.start()
        self.manifest_thread.start()

    def fix(self) :
        self.build_exonerate_index()
        # TODO would be better to attempt to use exonerate index 
        # (fails if index is built on another OS)

    def shutdown(self) :
        self.stop = True

    def join(self) :
        # if there is only alignments to do, then joining the alignment
        # threads results in ctrl-c being passed to an instance of prank
        # which is not correct behaviour
        #
        # instead just poll for completion
        while True :
            if self.alignment_queue.empty() or self.stop :
                break
            
            time.sleep(1)

        for t in self.alignment_threads :
            t.join()

        self.manifest_thread.join()

    def _consume_alignment_queue(self) :
        while not self.stop :
            try :
                fname = self.alignment_queue.get(timeout=type(self).queue_timeout)
            
            except Queue.Empty :
                if self.download_complete :
                    break
                continue

            self._align(fname)
            self.alignment_queue.task_done()

        self.alignment_queue.join()

        self.alignments_complete = True
        print "Info: alignment thread finished"

    def _consume_manifest_queue(self) :
        while not self.stop :
            try :
                data = self.manifest_queue.get(timeout=type(self).queue_timeout)
           
            except Queue.Empty :
                if self.alignments_complete :
                    break
                continue

            if len(data) != 3 :
                print >> sys.stderr, "Error: manifest queue contained %s" % str(data)
                sys.exit(-1)

            fname = self._write_file(data[0], data[1])

            if data[2] :
                self._add_to_alignment_queue(fname)

            self.manifest_queue.task_done()

        print "Info: manifest thread finished"

    def _contents(self, fname) :
        s = ""
        f = open(fname)
        
        for line in f :
            s += line

        f.close()
        return s

    def _write_file(self, filename, filecontents) :
        self.manifest.append_to_manifest(filename, filecontents)

        # write the actual file, we don't care if this gets interrupted
        # because then it will be discovered by recalculating the hash
        f = open(os.path.join(self.basedir, filename), 'w')
        f.write(filecontents)
        os.fsync(f)
        f.close()

        print "Info: written %s" % filename
        return os.path.join(self.basedir, filename)

    def _add_to_manifest_queue(self, filename, filecontents, align) :
        self.manifest_queue.put((filename, filecontents, align))

    def _add_to_alignment_queue(self, filename) :
        self.alignment_queue.put(filename)

    def _check_directory(self, dirname, create=False) :
        if not os.path.exists(dirname) :
            if create :
                try :
                    os.makedirs(dirname)
                    print "Info: created %s" % dirname
                except OSError, ose :
                    print >> sys.stderr, "Error: %s" % str(ose)
                    sys.exit(-1)
            else :
                print >> sys.stderr, "Error: '%s' does not exist." % dirname
                sys.exit(-1)

        elif not os.path.isdir(dirname) :
            print >> sys.stderr, "Error: '%s' exists, but is not a directory!" % dirname
            sys.exit(-1)

        else :
            pass
        
    def _reset_cache(self) :
        #shutil.rmtree(self.basedir, ignore_errors=True)

        #try:
        #    os.rmdir(os.path.dirname(self.basedir))
        #except OSError, ose :
        #    pass

        for fname in glob.glob(os.path.join(self.basedir, '*')) :
            os.remove(fname)

        open(self.manifest_name, 'w').close()

    def _swap_dirname(self, fname, directory=None) :
        return os.path.join(self.basedir if not directory else directory, os.path.basename(fname))

    def _count_sequences(self, fname) :
        count = 0
        for label,seq in MinimalFastaParser(open(fname)) :
            count += 1
        return count

    def _random_filename(self) :
        return os.path.basename(tempfile.mktemp(prefix=type(self).file_prefix, dir=self.basedir))

    def _align(self, infile) :
        outfile = self._swap_dirname(infile, directory=self.tmpdir)
        
        print "Prank: aligning %s ..." % (os.path.basename(infile))

        try :
            outfiles = Prank(self.tmpdir, self.prank).align(infile, outfile)
        
        except PrankError, pe :
            print >> sys.stderr, "Error: Prank died on %s ..." % (os.path.basename(infile))
            return

        except OSError, ose :
            print >> sys.stderr, "Error: '%s' %s" % (self.prank, str(ose))
            self.stop = True
            return

        for f in outfiles :
            self._add_to_manifest_queue(os.path.basename(f), self._contents(f), False)

        print "Info: aligned sequences in %s" % os.path.basename(infile)

    def build(self) :
        print "Info: enumerating gene families in %s release %d" % (self.species, self.release)
        genome = Genome(self.species, Release=self.release, account=self.account)
        
        if (self.species2 != None) and (self.database != 'ensembl') :
            print >> sys.stderr, "Warning: --species2 options can only be used with species found in ensembl-metazoa!"

        species_list = [self.species]

        if self.species2 is not None :
            species_list.append(self.species2)


        compara = Compara(species_list, Release=self.release, account=self.account)


        # DON'T TRY THIS AT HOME!
        #
        # what happens is it searches for compara databases, but unfortunately finds more than one
        # in this situation pycogent just connects to the first one, which is always compara_bacteria
        # so one solution is to dig through all the compara objects internals to provide a connection
        # to the correct database ... obviously not the best solution, but at 6 lines of code definitely 
        # the shortest ;-P
        #
        if self.database == 'ensembl-genomes' :
            from cogent.db.ensembl.host import DbConnection
            from cogent.db.ensembl.name import EnsemblDbName
            import sqlalchemy

            new_db_name = EnsemblDbName(compara.ComparaDb.db_name.Name.replace('bacteria', 'metazoa'))
            compara.ComparaDb._db = DbConnection(account=self.account, db_name=new_db_name)
            compara.ComparaDb._meta = sqlalchemy.MetaData(compara.ComparaDb._db)
        # end of DON'T TRY THIS AT HOME!


        skipped = 0
        for gene in genome.getGenesMatching(BioType='protein_coding') :
            stableid = gene.StableId.lower()

            # ignore genes that have already been seen as members of
            # gene families
            if stableid in self.genes :
                skipped += 1
                continue

            self.genes.add(stableid)

            # get cds sequences of any paralogs
            paralogs = compara.getRelatedGenes(StableId=stableid, Relationship='within_species_paralog')

            paralog_seqs = {}
            if paralogs is None :
                paralog_seqs[stableid] = gene.getLongestCdsTranscript().Cds
            else :
                for paralog in paralogs.Members :
                    paralog_id = paralog.StableId.lower()
                    self.genes.add(paralog_id)
                    paralog_seqs[paralog_id] = paralog.getLongestCdsTranscript().Cds

            # write md5 to the manifest and save to disk
            fname = self._random_filename()
            fcontents = ""
            for geneid in paralog_seqs :
                fcontents += (">%s\n%s\n" % (geneid, paralog_seqs[geneid]))

            self._add_to_manifest_queue(fname, fcontents, len(paralog_seqs) >= 2)

            print "Info: %s - %d gene%s in family (%s)" % (stableid, len(paralog_seqs), "s" if len(paralog_seqs) > 1 else "", fname)

            
            # XXX
            if len(self.genes) > 20 :
                break


            if self.species2 is not None :
                # get cds sequences of any orthologs
                ortholog_seqs = {}
                
                for geneid in paralog_seqs :
                    # XXX http://Nov2010.archive.ensembl.org/info/docs/compara/homology_method.html
                    for rel in ['ortholog_one2one', 'ortholog_one2many', 'ortholog_many2many'] :
                        orthologs = compara.getRelatedGenes(StableId=geneid, Relationship=rel)
                        if orthologs is not None :
                            for ortholog in orthologs.Members :
                                ortholog_id = ortholog.StableId.lower()
                                if (ortholog_id not in paralog_seqs) and (ortholog_id not in ortholog_seqs) :
                                    ortholog_seqs[ortholog_id] = ortholog.getLongestCdsTranscript().Cds

                # write md5 to manifest and save to disk
                fname = fname.replace("paralog", "ortholog")
                fcontents = ""
                for geneid in ortholog_seqs :
                    fcontents += (">%s\n%s\n" % (geneid, ortholog_seqs[geneid]))

                self._add_to_manifest_queue(fname, fcontents, False)

                print "Info: %s - %d orthologs (%s)" % (stableid, len(ortholog_seqs), fname)


            # check to see if we have been told to stop
            if self.stop :
                break


        if (not self.restart) and (skipped != 0) :
            print "Info: skipped over %d gene families that had already been downloaded." % skipped


        if self.stop :
            print "Info: killed by user..."
        else :
            print "Info: download complete..."
            self.download_complete = True
            self.join()
            self.build_exonerate_index()

            self._write_file('done', '')

            print "Info: done!"

    def build_exonerate_index(self) :
        pat = re.compile("^paralog_[" + string.ascii_letters + string.digits + "_" + "]{6}$")
        
        fa_name = os.path.join(self.basedir, 'exonerate.fa')
        db_name = os.path.join(self.basedir, 'exonerate.esd')
        in_name = os.path.join(self.basedir, 'exonerate.esi')

        # write everything into a single file
        # XXX unforunately some ensembl gene trees share genes (a minority), so for
        #     exonerate to work I either need to change the ids (possible) or else
        #     don't include all gene trees (better as there are so few affected)
        everything = open(fa_name, 'w') 
        geneset = set()

        for fname in glob.glob(os.path.join(self.basedir, '*')) :
            if pat.match(os.path.basename(fname)) :
                #f = open(fname)
                #print >> everything, f.read()
                #f.close()
                f = FastqFile(fname)
                f.open()
                tmp = []

                for seq in f :
                    # if any of the genes have been seen for far, then we 
                    # essentially exclude this gene family
                    if seq.id in geneset :
                        break

                    tmp.append(seq)
                else :
                    for i in tmp :
                        geneset.add(i.id)
                        print >> everything, i.fasta()

                f.close()

        everything.close()
        del geneset

        # fastareformat
        command = 'fastareformat %s > %s' % (fa_name, fa_name + '.tmp')
        ret = os.system(command)
        if ret != 0 :
            print >> sys.stderr, "Error: fastareformat returned error code %d" % ret
            sys.exit(-1)

        try :
            os.rename(fa_name + '.tmp', fa_name)
        
        except OSError, ose :
            print >> sys.stderr, "Error: %s" % str(ose)
            sys.exit(-1)

        # build the database
        command = 'fasta2esd %s %s &> /dev/null' % (fa_name, db_name)
        print "Info: building exonerate database" #(%s)" % command
        ret = os.system(command)
        if ret != 0 :
            print >> sys.stderr, "Error: fasta2esd returned error code %d" % ret
            sys.exit(-1)

        # build the index
        command = 'esd2esi %s %s &> /dev/null' % (db_name, in_name)
        print "Info: creating exonerate database index" #(%s)" % command
        ret = os.system(command)
        if ret != 0 :
            print >> sys.stderr, "Error: esd2esi returned error code %d" % ret
            sys.exit(-1)
