from glutton.base import ExternalTool
from glutton.utils import get_log

from sys import exit
import subprocess
import os


class Blast(ExternalTool) :
    def __init__(self) :
        super(Blast, self).__init__()

        self._results = []

    @property
    def version(self) :
        returncode, output = self._execute(["-version"], [])

        for line in output.split('\n') :
            if line.startswith(self.name) :
                v = line.strip().split()[-1]
                return v[:-1]

        raise ExternalToolError("could not get version of %s" % self.name)
    
    @staticmethod
    def makedb(db_fname, nucleotide=False) :
        c = ["makeblastdb", "-in", db_fname, "-dbtype", "nucl" if nucleotide else "prot"]

        try :
            get_log().debug(" ".join(c))
            subprocess.check_output(c, stderr=subprocess.STDOUT, close_fds=True)

        except subprocess.CalledProcessError, cpe :
            get_log().fatal("%s returncode=%d\n%s" % (c[0], cpe.returncode, cpe.output))
            exit(1)

    @property
    def results(self) :
        return self._results

    def run(self, query, database, outfile) :
        parameters = [
            "-query", query,
            "-db", database,
            "-out", outfile,
            "-max_target_seqs", "1",
            "-outfmt", "6"
            ]

        returncode, output = self._execute(parameters, [])

        with open(outfile) as f :
            for line in f :
                line = line.strip()

                if line == "" :
                    continue

                try :
                    contig,gene,identity,length = line.split()[:4]
                    identity = float(identity)
                    length = int(length)
                    self._results.append((contig,gene,identity,length))            

                except ValueError :
                    self.log.warn("bad line returned by %s (%s)" % (self.name, line))
                    continue


        return returncode

class Blastx(Blast) :
    def __init__(self) :
        super(Blastx, self).__init__()

class Tblastx(Blast) :
    def __init__(self) :
        super(Tblastx, self).__init__()

if __name__ == '__main__' :
    print Blastx().name, "version is", Blastx().version
    print Tblastx().name, "version is", Tblastx().version
