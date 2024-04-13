# HSYNC Server - HTTP-based file synchronization server
# Copyright (C) 2025 Alberto Basso
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""
API endpoints package for HSYNC Server.

This package contains all REST API and WebSocket endpoints.
"""

from hsync_server.api.utils import (
    compute_md5,
    get_dir_info,
    get_permissions,
    is_valid_bucket,
    is_valid_dir,
    set_permissions,
)