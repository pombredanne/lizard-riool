from django.conf import settings
from django.contrib.gis import geos
from django.db import connection
from lizard_map.coordinates import RD
from lizard_map.workspace import WorkspaceItemAdapter
from lizard_map.models import ICON_ORIGINALS
from lizard_map.symbol_manager import SymbolManager
from lizard_riool import models
import mapnik
import os
import re


# Colors from http://www.herethere.net/~samson/php/color_gradient/
CLASSES = (
    ('A', '0%-10%',   0.00, 0.10, '00ff00'),
    ('B', '10%-25%',  0.10, 0.25, '669900'),
    ('C', '25%-50%',  0.25, 0.50, '996600'),
    ('D', '50%-75%',  0.50, 0.75, 'CC3200'),
    ('E', '75%-100%', 0.75, 1.01, 'ff0000'))


def get_class_boundaries(pct):
    "Return the class and its boundaries for a given fraction."
    for klasse, _, min_pct, max_pct, _ in CLASSES:
        if pct >= min_pct and pct < max_pct:
            return klasse, min_pct, max_pct


GENERATED_ICONS = os.path.join(settings.MEDIA_ROOT, 'generated_icons')
SYMBOL_MANAGER = SymbolManager(ICON_ORIGINALS, GENERATED_ICONS)
RIOOL_ICON = 'pixel.png'
RIOOL_ICON_LARGE = 'pixel16.png'

DATABASE = settings.DATABASES['default']
PARAMS = {
    'host': DATABASE['HOST'],
    'port': DATABASE['PORT'],
    'user': DATABASE['USER'],
    'password': DATABASE['PASSWORD'],
    'dbname': DATABASE['NAME'],
    'srid': models.SRID,
}


def html_to_mapnik(color):
    r, g, b = color[0:2], color[2:4], color[4:6]
    rr, gg, bb = int(r, 16), int(g, 16), int(b, 16)

    return rr / 255.0, gg / 255.0, bb / 255.0, 1.0


def default_database_params():
    """Get default database params. Use a copy of the dictionary
    because it is mutated by the functions that use it."""
    return PARAMS.copy()


class RmbAdapter(WorkspaceItemAdapter):
    """Superclass for SUFRIB and SUBRMB WorkspaceItemAdapters.

    Both SUFRIB and SUBRMB files may have *RIOO and/or *PUT
    records, so visualization of these can be shared in a
    superclass.
    """

    javascript_hover_handler = 'popup_hover_handler'

    def __init__(self, *args, **kwargs):
        super(RmbAdapter, self).__init__(*args, **kwargs)
        self.id = int(self.layer_arguments['id'])  # upload_id

    def layer(self, layer_ids=None, request=None):
        "Return Mapnik layers and styles."
        layers, styles = [], {}

        # Add putten
        self._put_layer(layers, styles)

        # Add lost capacity
        sewer_style = mapnik.Style()

        for _, _, min_perc, max_perc, color in CLASSES:
            r, g, b, a = html_to_mapnik(color)

            icon = SYMBOL_MANAGER.get_symbol_transformed(
                RIOOL_ICON, color=(r, g, b, a))

            layout_rule = mapnik.Rule()
            symbol = mapnik.PointSymbolizer(
                os.path.join(GENERATED_ICONS, icon), "png", 16, 16)
            symbol.allow_overlap = True
            layout_rule.symbols.append(symbol)
            layout_rule.filter = mapnik.Filter(
                str("[value] >= %s and [value] < %s" % (min_perc, max_perc)))
            sewer_style.rules.append(layout_rule)

        styles["sewerStyle"] = sewer_style

        query = str("""(
            SELECT
                sg.flooded_percentage AS value,
                sg.xy AS xy
            FROM
                lizard_riool_storedgraph sg
            WHERE
                rmb_id=%s
            ) AS data""" % (self.id,))

        params = default_database_params()
        params['table'] = query
        params['geometry_field'] = 'xy'
        datasource = mapnik.PostGIS(**params)
        layer = mapnik.Layer('percentagesLayer', RD)
        layer.datasource = datasource
        layer.styles.append("sewerStyle")
        layers.append(layer)

        return layers, styles

    def _put_layer(self, layers, styles):
        """Moved the Put layer to its own function because it is also
        used by subclassing adapters."""

        # Visualization of "putten"

        # Put Style
        style = mapnik.Style()
        rule = mapnik.Rule()
        symbol = mapnik.PointSymbolizer()
        symbol.allow_overlap = True
        rule.symbols.append(symbol)
        style.rules.append(rule)
        styles['putStyle'] = style

        # Put Label Style
        style = mapnik.Style()
        rule = mapnik.Rule()
        rule.max_scale = 1700
        symbol = mapnik.TextSymbolizer('put_id', 'DejaVu Sans Book', 10,
            mapnik.Color('black'))
        symbol.allow_overlap = True
        symbol.label_placement = mapnik.label_placement.POINT_PLACEMENT
        symbol.vertical_alignment = mapnik.vertical_alignment.TOP
        symbol.displacement(0, -3)  # slightly above
        rule.symbols.append(symbol)
        style.rules.append(rule)
        styles['putLabelStyle'] = style

        # Datasource and Layer
        query = """(select * from lizard_riool_putten
            where upload_id=%d) data""" % self.id
        params = default_database_params()
        params['table'] = query
        params['geometry_field'] = 'the_geom'
        datasource = mapnik.PostGIS(**params)

        layer = mapnik.Layer('putLayer', RD)
        layer.datasource = datasource
        layer.maxzoom = 35000
        layer.styles.append('putStyle')
        layer.styles.append('putLabelStyle')
        layers.append(layer)

    def legend(self, updates=None):
        legend = []
        for classname, classdesc, _, _, color in CLASSES:
            r, g, b, a = html_to_mapnik(color)

            icon = SYMBOL_MANAGER.get_symbol_transformed(
                RIOOL_ICON_LARGE, color=(r, g, b, a))

            legend.append({
                    'img_url': os.path.join(
                        settings.MEDIA_URL, 'generated_icons', icon),
                    'description': "klasse %s (%s)" % (classname, classdesc),
                    })
        return legend

    def extent(self, identifiers=None):
        "Return the extent in Google projection"
        cursor = connection.cursor()
        cursor.execute("""
            select ST_Extent(ST_Transform(the_geom, 900913))
            from lizard_riool_putten where upload_id=%s
            """, [self.id])
        row = cursor.fetchone()
        box = re.compile('[(|\s|,|)]').split(row[0])[1:-1]
        return {
            'west': box[0], 'south': box[1],
            'east': box[2], 'north': box[3],
        }

    def search(self, x, y, radius=None):
        """We only use this for the mouse hover function; return the
        minimal amount of information necessary to show it."""

        pnt = geos.Point(x, y, srid=900913)
        points = (models.StoredGraph.objects.filter(rmb__id=self.id).
                  filter(xy__distance_lte=(pnt, radius)).
                  distance(pnt).
                  order_by('distance'))

        if not points:
            return []

        point = points[0]

        return [{
                'name': ('%.0f%% verloren berging' %
                         (point.flooded_percentage * 100,)),
                'stored_graph_id': point.pk,
                'distance': point.distance.m,
                }]
