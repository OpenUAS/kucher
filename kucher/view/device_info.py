#
# Copyright (C) 2018 Zubax Robotics OU
#
# This file is part of Kucher.
# Kucher is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
# Kucher is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty
# of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more details.
# You should have received a copy of the GNU General Public License along with Kucher.
# If not, see <http://www.gnu.org/licenses/>.
#
# Author: Pavel Kirienko <pavel.kirienko@zubax.com>
#

import datetime
from dataclasses import dataclass


@dataclass
class SoftwareVersion:
    major: int = 0
    minor: int = 0

    build_timestamp_utc: datetime.datetime = datetime.datetime.fromtimestamp(0)

    vcs_commit_id: int = 0
    image_crc: int = 0

    release_build: bool = False
    dirty_build: bool = False


@dataclass
class HardwareVersion:
    major: int = 0
    minor: int = 0


@dataclass
class BasicDeviceInfo:
    name:                        str = ''
    description:                 str = ''
    software_version:            SoftwareVersion = SoftwareVersion
    hardware_version:            HardwareVersion = HardwareVersion
    globally_unique_id:          bytes = b'\0' * 16
