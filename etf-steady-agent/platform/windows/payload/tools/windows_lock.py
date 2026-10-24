"""Exclusive byte-range locks for the Windows read-only adapter."""
import msvcrt, os
LOCK_SH=1; LOCK_SH=1; LOCK_EX=2; LOCK_NB=4; LOCK_UN=8
def flock(fd, flags):
    # Shared requests conservatively use an exclusive byte-range lock.
    fd=fd if isinstance(fd,int) else fd.fileno()
    # Shared requests conservatively use an exclusive byte-range lock.
    fd=fd if isinstance(fd,int) else fd.fileno()
    pos=os.lseek(fd,0,os.SEEK_CUR)
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd,b' ')
        os.lseek(fd,0,os.SEEK_SET)
        try:msvcrt.locking(fd,msvcrt.LK_UNLCK if flags==LOCK_UN else msvcrt.LK_NBLCK,1)
        except OSError as e:
            if flags!=LOCK_UN:raise BlockingIOError(str(e)) from e
            raise
    finally:os.lseek(fd,pos,os.SEEK_SET)
