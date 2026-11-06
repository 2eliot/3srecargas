"""Estados de una orden y qué reservan.

Un pago (su referencia bancaria y su id de pago de Pabilo) queda RESERVADO por las órdenes que lo tienen contado: no se puede
usar en otra orden. Antes la lista de estados que reservan estaba repetida suelta en varios archivos; vive aquí para que un
estado nuevo no se quede fuera de ninguno.

'refunded' (devuelta) SIGUE reservando: el dinero ya se le regresó al cliente y ese pago queda quemado para siempre. 'rejected'
(rechazada) en cambio libera el pago.
"""

# Estados en los que el pago de una orden cuenta como YA USADO.
LIVE_PAYMENT_STATUSES = ('pending', 'approved', 'completed', 'refunded')

# Estados desde los que el admin puede hacer una devolución. Nunca una orden aprobada o completada: esas ya recibieron la recarga
# (y los puntos y la comisión se dan al completarse), así que no hay nada que devolver sin quitarle lo entregado.
REFUNDABLE_STATUSES = ('pending', 'rejected')
