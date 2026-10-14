"""Bounded structural metadata only: never retain primitive values or blob bytes."""
from zasim.trade_protocol import ArchiveReader

def archive_shape(data):
    ar=ArchiveReader(data);events=[]
    def node(depth):
        if depth>6:raise ValueError('shape_depth')
        tag=ar.data[ar.offset]
        if tag==0xb9:
            n=ar.read_struct();events.append('struct'+str(n))
            if n>32:raise ValueError('shape_fields')
            return 'struct'+str(n)+'('+','.join(node(depth+1) for _ in range(n))+')'
        if tag==0xbc:
            value='blob'+str(len(ar.read_blob()));events.append(value);return value
        if tag==0xba:
            ar._byte('array marker');n=ar.read_varuint();events.append('array'+str(n))
            if n>32:raise ValueError('shape_array_bounds')
            return 'array'+str(n)+'('+','.join(node(depth+1) for _ in range(n))+')'
        if tag<=0x7f or 0x80<=tag<=0x83:
            before=ar.offset;ar.read_varuint();value='uint'+str(ar.offset-before);events.append(value);return value
        if tag in (0x84,0x85,0x86) or tag>=0xc0:
            before=ar.offset;ar.read_signed();value='sint'+str(ar.offset-before);events.append(value);return value
        raise ValueError('shape_tag_'+str(tag))
    try:
        shape=node(0);ar.finish();return shape
    except (ValueError,IndexError) as e:
        # The allowlisted error is our own structural classification, not the
        # archive reader's exception arguments or raw field values.
        category=str(e) if type(e) is ValueError and str(e).startswith('shape_') else type(e).__name__
        return 'prefix('+','.join(events[:64])+'):partial_at_'+str(ar.offset)+':'+category
