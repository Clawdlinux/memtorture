from memproof.types import NotSupported


class Base:
    server_pid = None
    notes = ""

    def retrieve_for(self, ctx):
        raise NotSupported("Tier 2 is outside B1")

    def data_dirs(self):
        return [self.workdir]

    def stop(self, target="all", kill=False):
        pass
