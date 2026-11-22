#!/usr/bin/python3
"""Complete bootc's loop inventory with the partition table on that device."""

import json
import os
import re
import subprocess
import sys


def complete_partition_types(device, table):
    if table['label'] != 'gpt' or table['device'] != device['path']:
        raise ValueError('not the requested loop device GPT')
    types = {part['node']: part['type'] for part in table['partitions']}
    for child in device['children']:
        if child['type'] == 'part' and not child['parttype']:
            child['parttype'] = types[child['path']]


def main(args):
    lsblk = '/usr/bin/lsblk'
    if len(args) != 4 or args[:3] != ['-J', '-b', '-O'] or not re.fullmatch(r'/dev/loop[0-9]+', args[3]):
        os.execv(lsblk, [lsblk, *args])
    disk = args[3]
    tree = json.loads(subprocess.check_output([lsblk, '--properties-by', 'blkid,udev', *args]))
    for device in tree['blockdevices']:
        if device['path'] == disk and any(
            child['type'] == 'part' and not child['parttype'] for child in device.get('children', ())
        ):
            table = json.loads(subprocess.check_output(['sfdisk', '--json', '--', disk]))['partitiontable']
            complete_partition_types(device, table)
    json.dump(tree, sys.stdout)


if __name__ == '__main__':
    main(sys.argv[1:])
