import jwt
from app.settings import VALIDATOR_KEY, VALIDATOR_ISSUER 


def validateToken(token):
    try:
        if not token:
            print('No token')
            return False
        print(token)
        token_data = jwt.decode(token, VALIDATOR_KEY, algorithms="HS256")
        print(token_data)
        if 'iss' in token_data and token_data['iss'] == VALIDATOR_ISSUER:
            return token_data['data']
        print('Token issuer is invalid')
        return False
    except jwt.ExpiredSignatureError:
        print("Token expired")
        return False
